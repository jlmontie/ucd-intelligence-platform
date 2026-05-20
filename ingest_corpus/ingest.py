#!/usr/bin/env python3
"""
Ingestion pipeline for UCD magazine PDFs.

For each issue:
  1. Render pages to JPEG images and upload to GCS
  2. LLM pass: segment pages into articles, store article rows + content_hash
  3. Probe runner: run project_panel_v1, claims_v1, quotes_v1 per article.
     Probe outputs land in `probe_runs` (cached by content_hash).
  4. Materialize probe outputs into projects / firms / roles / claims / quotes.

`--reprocess` is non-destructive: existing issue/article rows are kept;
only the probe runner re-runs (and is itself a no-op when the article's
content_hash and probe versions are unchanged).

Usage:
    python ingest.py --issues_dir ../issues/
    python ingest.py --pdfs ../issues/UC-D+February+2026-spreads.pdf
    python ingest.py --issues_dir ../issues/ --reprocess
    python ingest.py --issues_dir ../issues/ --limit 3   # validate first 3 issues
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv
from google.cloud import storage
from pdf2image import convert_from_path
from PIL import Image
from tqdm import tqdm

from core.db import dict_cur, get_conn
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response
from core.probes import REGISTRY, run_probe_for_article
from core.resolution.resolve_firms import deterministic_match
from core.resolution.resolve_people import upsert_person
from core.resolution.resolve_projects import resolve_project

load_dotenv()

IMAGE_DPI = 150
GCS_BUCKET = "uc-and-d-assets"
GCS_IMAGES_PREFIX = "page_images"

# Article types that don't carry project content. Probes skip these
# so we don't burn LLM calls on Publisher's Message, Industry News,
# A/E/C People columns, advertisements, indexes, etc.
NON_PROBED_ARTICLE_TYPES = frozenset({"advertisement", "column", "other"})


# ── GCS helpers ──────────────────────────────────────────────────────────────

def gcs_client() -> storage.Client:
    return storage.Client()


def upload_image(gcs: storage.Client, local_path: Path, gcs_path: str) -> str:
    bucket = gcs.bucket(GCS_BUCKET)
    blob = bucket.blob(gcs_path)
    if not blob.exists():
        blob.upload_from_filename(str(local_path), content_type="image/jpeg")
    return f"gs://{GCS_BUCKET}/{gcs_path}"


# ── Image helpers ─────────────────────────────────────────────────────────────

def render_and_upload(pdf_path: Path, issue_id: int, gcs: storage.Client) -> list[str]:
    """Render PDF to JPEGs, upload to GCS. Returns list of gs:// URIs."""
    tmp_dir = Path("/tmp") / f"ucd_issue_{issue_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    pages = convert_from_path(pdf_path, dpi=IMAGE_DPI, fmt="jpeg")
    uris = []
    for i, img in enumerate(pages, start=1):
        local = tmp_dir / f"page_{i:04d}.jpg"
        img.save(local, "JPEG", quality=85)
        gcs_path = f"{GCS_IMAGES_PREFIX}/{issue_id}/page_{i:04d}.jpg"
        uri = upload_image(gcs, local, gcs_path)
        uris.append(uri)

    return uris


def extract_page_texts(pdf_path: Path) -> list[str]:
    """Extract embedded text from each PDF page via pdfplumber."""
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def uri_to_b64(uri: str, gcs: storage.Client, max_width: int = 1568) -> str:
    """Download a GCS image and return base64-encoded JPEG."""
    path = uri.removeprefix(f"gs://{GCS_BUCKET}/")
    blob = gcs.bucket(GCS_BUCKET).blob(path)
    data = blob.download_as_bytes()
    img = Image.open(BytesIO(data))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode()


def image_content(uri: str, gcs: storage.Client) -> dict:
    b64 = uri_to_b64(uri, gcs)
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
    }


# ── Stage 1: Article segmentation ─────────────────────────────────────────────

SEGMENT_PROMPT = """You are segmenting a construction magazine issue into articles.

Each page is provided as extracted PDF text followed by the page image. The extracted text is
verbatim from the PDF — treat it as authoritative for specific words, numbers, and names.
Use the image for visual layout context (headlines, article breaks, advertisement boundaries).

Group consecutive pages that belong to the same article or feature, and identify:
- page range (start and end, inclusive)
- article title (from the headline visible on the page)
- author byline if visible
- whether this is a project feature article, a department/column, an advertisement, or other

Return a JSON array. Each element:
{
  "page_start": <int>,
  "page_end": <int>,
  "title": <string or null>,
  "author": <string or null>,
  "type": "project_feature" | "column" | "advertisement" | "other"
}

Rules:
- A project feature article typically spans 2-6 pages and includes a project info panel.
- Advertisements are single pages with no editorial content.
- If a spread page shows two distinct articles, split them into separate entries.
- Return ONLY valid JSON, no commentary.
"""


def segment_issue(model: str, page_uris: list[str], page_texts: list[str], gcs: storage.Client, batch_size: int = 5) -> list[dict]:
    all_segments = []

    for batch_start in range(0, len(page_uris), batch_size):
        batch_uris = page_uris[batch_start:batch_start + batch_size]
        batch_texts = page_texts[batch_start:batch_start + batch_size]

        content = [{"type": "text", "text": SEGMENT_PROMPT}]
        for i, (uri, text) in enumerate(zip(batch_uris, batch_texts, strict=True)):
            page_num = batch_start + i + 1
            content.append({"type": "text", "text": f"\n--- Page {page_num} (extracted text) ---\n{text}"})
            content.append({"type": "text", "text": f"--- Page {page_num} (image) ---"})
            content.append(image_content(uri, gcs))

        raw = call_llm(model, [{"role": "user", "content": content}])
        try:
            result = parse_json_response(raw)
        except (json.JSONDecodeError, ValueError) as e:
            tqdm.write(f"  WARNING: segmentation parse error (pages {batch_start+1}-{batch_start+len(batch_uris)}): {e}")
            continue
        # Models sometimes wrap the array in {"segments": [...]} or
        # {"articles": [...]} despite the prompt asking for a bare
        # array. Unwrap if so, then filter to dict-shaped items —
        # nested arrays inside the result list otherwise crash later
        # at `seg.get(...)` with 'list has no attribute get'.
        if isinstance(result, dict):
            result = result.get("segments") or result.get("articles") or []
        if not isinstance(result, list):
            tqdm.write(f"  WARNING: segmenter returned non-array (type={type(result).__name__}); skipping batch")
            continue
        all_segments.extend(s for s in result if isinstance(s, dict))

    return all_segments


# ── Stage 2: Probe-driven extraction ──────────────────────────────────────────
#
# Extraction is no longer a single inline LLM call; it's three probes
# (project_panel_v1, claims_v1, quotes_v1) run via core.probes.runner.
# This function wraps the runner with the page-text and image loaders
# specific to corpus articles, then materializes the cached probe
# outputs into projects / firms / roles / claims / quotes.


def compute_article_hash(page_texts: list[str]) -> str:
    h = hashlib.sha256()
    for t in page_texts:
        h.update((t or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


_MONTHS_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
# Abbreviations indexed parallel to _MONTHS_FULL. "Sep" alone, "Sept"
# common in feature copy, both should map to September.
_MONTHS_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sept?", "Oct", "Nov", "Dec",
)


def parse_issue_filename(filename: str) -> tuple[int | None, str | None]:
    """Best-effort parse of (year, month_label) from an issue PDF filename.

    The corpus filenames are inconsistent — `UC-D+February+2026-spreads.pdf`,
    `2020_APRIL20.spreads-2.pdf`, `aug | sept_spreads_2016.pdf`, etc. —
    so this is heuristic. Returns `(None, None)` on failure; the caller
    can fall back to OCR'ing the cover page.

    Year is the first 19xx/20xx in the filename. Month preference: full
    name first (avoids `Mar` matching firm names like `Marquardt`), then
    abbreviation. Double-issues like `aug | sept` resolve to the first
    month (August) by convention — adjust manually after ingest if the
    second month is more representative.
    """
    year_match = re.search(r"(19|20)\d{2}", filename)
    year = int(year_match.group()) if year_match else None

    # Boundary rules:
    #   full names — must not be flanked by letters (avoids false hits
    #     mid-word). Underscores, digits, punctuation are fine.
    #   abbreviations — left side same; right side allows uppercase
    #     (CamelCase boundary), so `DecSpreads` matches `Dec` while
    #     `Decision` does not (next char `i` is lowercase).
    def _match_full(token: str) -> bool:
        return bool(re.search(rf"(?<![a-zA-Z]){token}(?![a-zA-Z])",
                              filename, re.IGNORECASE))

    def _match_abbr(token: str) -> bool:
        # Anchored case-sensitive on the right side to enforce the
        # CamelCase boundary; the abbreviation itself stays case-
        # insensitive on its own characters.
        pattern = rf"(?<![a-zA-Z])(?i:{token})(?![a-z])"
        return bool(re.search(pattern, filename))

    for full in _MONTHS_FULL:
        if _match_full(full):
            return year, full

    for full, abbr in zip(_MONTHS_FULL, _MONTHS_ABBR, strict=True):
        if _match_abbr(abbr):
            return year, full

    return year, None


def upsert_firm(cur, raw_name: str) -> int:
    """Record a firm mention and return a firm id.

    Routes through core.resolution.resolve_firms.deterministic_match so
    exact / normalized / alias / high-trigram (>=0.92) matches are
    caught at ingest time without an LLM call. Mentions that don't
    match deterministically get a placeholder firm row plus a
    firm_mentions entry with NULL confidence; the standalone
    `python -m core.resolution.resolve_firms` pass picks those up
    later, runs the LLM tiebreaker, and re-points canonical_id at the
    real canonical firm (or accepts the placeholder as canonical).
    """
    firm_id, confidence, _candidates = deterministic_match(cur, raw_name)
    if firm_id is None:
        # Race-safe insert: deterministic_match can miss when the
        # firm name normalizes to empty (e.g. "Architects, Inc." —
        # every token is in _FIRM_SUFFIXES). ON CONFLICT DO NOTHING
        # + RETURNING + fallback SELECT handles that case as well as
        # any actual race with a concurrent inserter.
        cur.execute(
            """
            INSERT INTO firms (name) VALUES (%s)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            (raw_name,),
        )
        row = cur.fetchone()
        if row:
            firm_id = row["id"]
        else:
            cur.execute("SELECT id FROM firms WHERE name = %s", (raw_name,))
            firm_id = cur.fetchone()["id"]
        confidence = None  # signals "needs the LLM resolver"
    cur.execute(
        """
        INSERT INTO firm_mentions (raw_text, canonical_id, confidence)
        VALUES (%s, %s, %s)
        """,
        (raw_name, firm_id, confidence),
    )
    return firm_id


def _latest_probe_output(cur, article_id: int, probe_name: str) -> dict | None:
    cur.execute(
        """
        SELECT pr.output_json
        FROM probe_runs pr
        JOIN probes p ON p.id = pr.probe_id
        WHERE pr.article_id = %s AND p.name = %s
        ORDER BY pr.ran_at DESC
        LIMIT 1
        """,
        (article_id, probe_name),
    )
    row = cur.fetchone()
    return row["output_json"] if row else None


_MAGNITUDE = {
    "thousand": 1_000,
    "k":        1_000,
    "million":  1_000_000,
    "mil":      1_000_000,
    "mm":       1_000_000,
    "m":        1_000_000,
    "billion":  1_000_000_000,
    "bil":      1_000_000_000,
    "b":        1_000_000_000,
}


_BYLINE_PREFIX = re.compile(r"^\s*(by\s+|author\s*:\s*)", re.IGNORECASE)


def _clean_byline(s: str | None) -> str | None:
    """Strip 'By ' / 'Author: ' prefixes that the project_panel_v1
    probe sometimes leaves on `author`. Returns None for empty/Staff.
    Defensive cleanup — the prompt asks for the bare name but the
    model occasionally echoes the surrounding label, so we normalize
    here rather than bumping the probe version every time."""
    if not s:
        return None
    cleaned = _BYLINE_PREFIX.sub("", str(s)).strip().strip(",")
    if not cleaned or cleaned.lower() == "staff":
        return None
    return cleaned


_FIRST_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _parse_int(s) -> int | None:
    """Parse a money/area/count string into an int.

    Handles literal digits ('$45,900,000' -> 45_900_000) and shorthand
    magnitudes ('$100 million' -> 100_000_000, '$5B' -> 5_000_000_000,
    '$1.5 million' -> 1_500_000). When no magnitude is present, takes
    the FIRST contiguous number from the string — earlier versions
    stripped every non-digit, which silently concatenated multiple
    numbers ('300,000 SF, including 130,000 SF' -> 300000130000,
    overflowing sq_ft INTEGER and crashing the corpus run mid-stream).

    Returns None for non-numeric input."""
    if not s:
        return None
    text = str(s).lower()
    m = re.search(
        r"([\d,]+(?:\.\d+)?)\s*(thousand|million|billion|mil|bil|mm|[kmb])\b",
        text,
    )
    if m:
        num = float(m.group(1).replace(",", ""))
        mult = _MAGNITUDE[m.group(2)]
        return int(round(num * mult))
    m = _FIRST_NUMBER.search(text)
    if not m:
        return None
    raw = m.group().replace(",", "")
    return int(round(float(raw))) if "." in raw else int(raw)


_PG_INT_MAX = 2_147_483_647   # PostgreSQL INTEGER upper bound


def _safe_int(value, *, lo: int = 0, hi: int = _PG_INT_MAX) -> int | None:
    """Clamp parsed ints to a sane range. Returns None for non-numeric
    or out-of-range input. Tolerates strings ('12'), floats (12.0),
    and embedded values ('p. 12' falls through to None via int()
    raising ValueError on the leading non-digit)."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _coerce_state(value) -> str | None:
    """projects.state is CHAR(2). The probe asks for a 2-letter code,
    but the model occasionally returns the full name ('Utah') which
    would crash the insert with 'value too long for type character(2)'.
    Drop anything that isn't already 2 alpha characters — better to
    lose a bad value than crash a 14-hour run."""
    if not isinstance(value, str):
        return None
    s = value.strip().upper()
    return s if len(s) == 2 and s.isalpha() else None


def _list_of_dicts(value) -> list[dict]:
    """Normalize a probe-output list field to a list of dicts. Probes
    occasionally return None, a stringified array, or a single dict
    instead of a list — this drops anything that won't iterate
    cleanly so a malformed batch doesn't kill the run."""
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _candidate_from_panel(proj_data: dict, article_id: int) -> dict:
    """Map a project_panel_v1 `project` block to the candidate dict
    that core.resolution.resolve_project expects. The (source_type,
    source_ref) pair makes resolve_project idempotent against replays
    of the same article.

    Numeric fields are clamped to fit their column types: `sq_ft` and
    `year_completed` are INTEGER (max 2.1B); `cost_usd` is BIGINT
    (rarely overflows for any plausible cost)."""
    cost_str = proj_data.get("cost")
    sq_str   = proj_data.get("square_footage")
    return {
        "name":             proj_data.get("name"),
        "typology":         proj_data.get("typology"),
        "location":         proj_data.get("location"),
        "city":             proj_data.get("city"),
        "state":            _coerce_state(proj_data.get("state")),
        "cost":             cost_str,
        "cost_usd":         _parse_int(cost_str),
        "square_footage":   sq_str,
        "sq_ft":            _safe_int(_parse_int(sq_str)),
        "stories_levels":   proj_data.get("stories_levels"),
        "delivery_method":  proj_data.get("delivery_method"),
        "year_completed":   _safe_int(proj_data.get("year_completed"),
                                      lo=1800, hi=2100),
        "status":           proj_data.get("status"),
        "source":           "corpus",
        "source_type":      "article",
        "source_ref":       str(article_id),
    }


_PAREN_GROUP = re.compile(r"\(([^)]*)\)")
_PAREN_STRIP = re.compile(r"\s*\([^)]*\)\s*")


def _extract_scope(text: str | None) -> tuple[str | None, str | None]:
    """Split a probe-output string into (clean, scope).

    Probes occasionally tag scope onto the wrong side of the
    (firm, role) split — `firm="Flynn Companies (patching)"` instead
    of `role="Roofing (patching)"`. Pulling the parenthetical content
    out of both fields and stashing it in `roles.scope` lets the firm
    and role strings stay canonical (so the natural-key UNIQUE
    collapses re-extraction variants) while preserving the scope
    detail. Returns `(None, None)` for empty input.
    """
    if not text:
        return text, None
    parens = [m.group(1).strip() for m in _PAREN_GROUP.finditer(text)
              if m.group(1).strip()]
    clean = _PAREN_STRIP.sub(" ", text).strip()
    scope = "; ".join(parens) if parens else None
    return (clean or None), scope


def _enumerate_team_roles(panel: dict):
    """Yield (firm_name, role_label, team) tuples from a project_panel_v1
    output. Owner is conventionally a single firm; design/construction
    teams are lists of {firm, role}.

    Defensive against malformed probe output: the JSON schema declares
    `firm` and `role` required on each team entry, but the model
    occasionally drops one — caught mid-corpus by a `KeyError: 'role'`
    on a construction_team entry. Silent-skipping incomplete entries
    keeps a single bad row from killing a 16-hour ingest. The probe
    cache still has the raw output for forensics."""
    owner = panel.get("owner")
    if isinstance(owner, str) and owner.strip():
        firm_clean, scope = _extract_scope(owner)
        if firm_clean:
            yield (firm_clean, "Owner", "owner", scope)
    for team_label, key in (("design", "design_team"),
                            ("construction", "construction_team")):
        for entry in _list_of_dicts(panel.get(key)):
            firm_raw = entry.get("firm")
            role_raw = entry.get("role")
            if not (firm_raw and role_raw):
                tqdm.write(
                    f"    skip incomplete {key} entry: "
                    f"firm={firm_raw!r} role={role_raw!r}"
                )
                continue
            firm_clean, firm_scope = _extract_scope(firm_raw)
            role_clean, role_scope = _extract_scope(role_raw)
            if not (firm_clean and role_clean):
                continue
            scope_parts = [s for s in (firm_scope, role_scope) if s]
            scope = "; ".join(scope_parts) if scope_parts else None
            yield (firm_clean, role_clean, team_label, scope)


def materialize_from_probes(conn, article_id: int) -> tuple[int | None, int, int]:
    """Read the latest probe outputs for `article_id` and write them
    into projects / firms / roles / claims / quotes.

    Project resolution flows through core.resolution.resolve_project so
    that re-extraction matches an existing canonical project instead of
    creating a duplicate (the bug surfaced by Q1 of the schema-smoke
    notebook). Article-scoped rows (claims, quotes) are wiped before
    re-insert so a probe-version bump can drop stale items; roles are
    inserted with ON CONFLICT DO NOTHING and rely on the §2.6
    natural-key UNIQUE for idempotency."""
    with dict_cur(conn) as cur:
        panel       = _latest_probe_output(cur, article_id, "project_panel_v1") or {}
        claims_out  = _latest_probe_output(cur, article_id, "claims_v1") or {}
        quotes_out  = _latest_probe_output(cur, article_id, "quotes_v1") or {}

        # Article-scoped: claims and quotes belong to exactly one
        # article, so dropping and re-inserting is the natural way to
        # handle a probe-version bump.
        cur.execute("DELETE FROM claims WHERE article_id=%s", (article_id,))
        cur.execute("DELETE FROM quotes WHERE article_id=%s", (article_id,))
    conn.commit()

    project_id = None
    raw_project = panel.get("project")
    proj_data = raw_project if isinstance(raw_project, dict) else {}
    if proj_data and proj_data.get("name"):
        candidate = _candidate_from_panel(proj_data, article_id)
        project_id, was_created, _conf = resolve_project(conn, candidate)

        # Maintain the legacy projects.source_article_id pointer for
        # newly-minted projects so existing readers keep working. On a
        # match, leave the existing pointer alone.
        if was_created:
            with dict_cur(conn) as cur:
                cur.execute(
                    "UPDATE projects SET source_article_id=%s WHERE id=%s",
                    (article_id, project_id),
                )
            conn.commit()

        with dict_cur(conn) as cur:
            for firm_name, role_label, team, scope in _enumerate_team_roles(panel):
                firm_id = upsert_firm(cur, firm_name)
                cur.execute(
                    """
                    INSERT INTO roles (project_id, firm_id, role, team, raw_name, scope)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT uniq_roles_natural_key DO NOTHING
                    """,
                    (project_id, firm_id, role_label, team, firm_name, scope),
                )
        conn.commit()

    n_claims = 0
    n_quotes = 0
    with dict_cur(conn) as cur:
        for claim in _list_of_dicts(claims_out.get("claims")):
            text = claim.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            cur.execute(
                """
                INSERT INTO claims (article_id, project_id, text, type, page)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project_id, article_id, md5(text)) DO NOTHING
                """,
                (article_id, project_id, text,
                 claim.get("type"),
                 _safe_int(claim.get("page"), lo=1, hi=10000)),
            )
            n_claims += cur.rowcount

        for q in _list_of_dicts(quotes_out.get("quotes")):
            text = q.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            speaker_name  = q.get("speaker_name")
            speaker_title = q.get("speaker_title")
            speaker_firm  = q.get("speaker_firm")

            # Resolve the speaker to a canonical person row so the
            # quote points at people.id, not just a free-text name.
            # `firm_id` is the canonical firm the speaker is attached
            # to; left NULL for unaffiliated speakers.
            firm_id = upsert_firm(cur, speaker_firm) if speaker_firm else None
            speaker_person_id = upsert_person(
                cur, speaker_name, speaker_title, speaker_firm, firm_id,
            )

            cur.execute(
                """
                INSERT INTO quotes
                    (article_id, project_id, speaker_name, speaker_title,
                     speaker_firm, speaker_person_id, text, page)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, article_id, md5(text), speaker_name) DO NOTHING
                """,
                (article_id, project_id, speaker_name, speaker_title,
                 speaker_firm, speaker_person_id, text,
                 _safe_int(q.get("page"), lo=1, hi=10000)),
            )
            n_quotes += cur.rowcount

        # Author preserved across re-runs: only fill it if a previous
        # run already extracted one and the new run regressed to null.
        cur.execute(
            """
            UPDATE articles
            SET primary_project_id = %s,
                summary            = %s,
                author             = COALESCE(%s, author)
            WHERE id = %s
            """,
            (project_id, panel.get("summary"),
             _clean_byline(panel.get("author")), article_id),
        )
        if project_id:
            cur.execute(
                """
                INSERT INTO article_projects (article_id, project_id, is_primary)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (article_id, project_id) DO UPDATE SET is_primary = TRUE
                """,
                (article_id, project_id),
            )
    conn.commit()

    return project_id, n_claims, n_quotes


# ── Main pipeline ──────────────────────────────────────────────────────────────

def _fetch_articles_for_issue(cur, issue_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT id, page_start, page_end, title, article_type, content_hash
        FROM articles
        WHERE issue_id = %s
        ORDER BY page_start
        """,
        (issue_id,),
    )
    return cur.fetchall()


def _run_probes_for_article(
    conn,
    article: dict,
    page_texts_all: list[str],
    page_uris_all: list[str] | None,
    *,
    model: str,
) -> None:
    """Run every active probe against one article. If `page_uris_all`
    is None, probes run text-only — cheaper and useful for smoke tests
    since the prompts already treat the image as visual-context-only."""
    p_start = article["page_start"]
    p_end = article["page_end"]
    article_texts = page_texts_all[p_start - 1:p_end]
    article_uris = (
        page_uris_all[p_start - 1:p_end] if page_uris_all is not None else None
    )

    for spec in REGISTRY.values():
        run_probe_for_article(
            conn, spec, article, article_texts,
            model=model, image_uris=article_uris,
        )


def _segment_and_extract(
    issue_id: int,
    pdf_path: Path,
    model: str,
    conn,
    gcs: storage.Client,
    stats: dict,
    *,
    use_images: bool = True,
) -> dict:
    """Render + segment + create articles + run probes + materialize.

    Shared between the fresh-ingest path and the orphan-resume path
    (issue row exists but a prior run died before articles got created,
    typically because of a transient LLM quota / network error)."""
    filename = pdf_path.name

    tqdm.write(f"  Rendering + uploading pages: {filename}")
    page_uris = render_and_upload(pdf_path, issue_id, gcs)
    page_texts = extract_page_texts(pdf_path)

    with dict_cur(conn) as cur:
        cur.execute("UPDATE issues SET page_count=%s WHERE id=%s", (len(page_uris), issue_id))
    conn.commit()

    tqdm.write(f"  Segmenting {len(page_uris)} pages...")
    segments = segment_issue(model, page_uris, page_texts, gcs)
    tqdm.write(f"  Found {len(segments)} segments")

    n_pages = len(page_texts)

    def _coerce_page(value, default: int) -> int:
        """Segmenter occasionally returns page numbers as strings
        ('"page_start": "12"' instead of 12) despite the schema
        declaring integer. Coerce defensively so a single bad
        segment doesn't kill the whole corpus run. Out-of-range
        or non-numeric values fall back to `default`."""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return n if 1 <= n <= n_pages else default

    for seg in segments:
        if not isinstance(seg, dict):
            # segment_issue should have filtered these out, but a stray
            # nested-array item still kills the issue. Skip-with-warn
            # rather than crashing the whole corpus run.
            tqdm.write(f"  skip non-dict segment: {type(seg).__name__}")
            continue
        p_start = _coerce_page(seg.get("page_start"), 1)
        p_end   = _coerce_page(seg.get("page_end"),   p_start)
        if p_end < p_start:
            p_end = p_start
        article_texts = page_texts[p_start - 1:p_end]
        content_hash = compute_article_hash(article_texts)

        with dict_cur(conn) as cur:
            cur.execute(
                """
                INSERT INTO articles
                    (issue_id, page_start, page_end, title, author,
                     article_type, content_hash, ingested_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                (issue_id, p_start, p_end, seg.get("title"), seg.get("author"),
                 seg.get("type"), content_hash, datetime.now(UTC)),
            )
            article_id = cur.fetchone()["id"]
        conn.commit()
        stats["articles"] += 1

        if seg.get("type") in NON_PROBED_ARTICLE_TYPES:
            continue

        tqdm.write(f"    Probing pages {p_start}-{p_end}: {(seg.get('title') or '(untitled)')[:60]}")
        article = {
            "id": article_id, "page_start": p_start, "page_end": p_end,
            "content_hash": content_hash, "title": seg.get("title"),
        }
        try:
            _run_probes_for_article(
                conn, article, page_texts,
                page_uris if use_images else None,
                model=model,
            )
            project_id, n_claims, n_quotes = materialize_from_probes(conn, article_id)
        except Exception as exc:
            # One bad article shouldn't kill a multi-hour corpus pass.
            # Roll back the article's partial transaction so the next
            # article starts clean, log, and continue.
            conn.rollback()
            stats.setdefault("article_failures", 0)
            stats["article_failures"] += 1
            tqdm.write(
                f"    SKIP article_id={article_id} after error: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if project_id:
            stats["projects"] += 1
        stats["claims"] += n_claims
        stats["quotes"] += n_quotes

    return stats


def ingest_issue(
    pdf_path: Path,
    model: str,
    conn,
    gcs: storage.Client,
    force: bool = False,
    use_images: bool = True,
) -> dict:
    """Ingest a single issue.

    Three states:
      - issue row absent → fresh ingest (insert issue + segment + extract).
      - issue row present, 0 articles → orphan from a prior run that died
        mid-segmentation; resume by re-segmenting against the existing
        issue_id. Auto-heals; no flag needed.
      - issue row present with articles → SKIP, unless `force` (--reprocess)
        in which case re-run probes against the existing articles.

    The destructive cascade-delete on the prior reprocess path was removed —
    see plan §2.4.
    """
    filename = pdf_path.name
    stats = {"articles": 0, "projects": 0, "claims": 0, "quotes": 0}

    with dict_cur(conn) as cur:
        cur.execute(
            """
            SELECT i.id,
                   (SELECT COUNT(*) FROM articles WHERE issue_id = i.id) AS n_articles
            FROM issues i
            WHERE i.filename = %s
            """,
            (filename,),
        )
        existing = cur.fetchone()

    if existing and existing["n_articles"] == 0:
        tqdm.write(
            f"  Resuming {filename}: issue row exists but has 0 articles "
            "(prior run died before segmentation completed)."
        )
        return _segment_and_extract(
            existing["id"], pdf_path, model, conn, gcs, stats,
            use_images=use_images,
        )

    if existing and not force:
        tqdm.write(f"  SKIP {filename} (already ingested; use --reprocess to re-run probes)")
        return stats

    if existing and force:
        return _reprocess_existing_issue(
            existing["id"], pdf_path, model, conn, gcs, stats,
            use_images=use_images,
        )

    # Fresh ingest path.
    year, month_label = parse_issue_filename(filename)
    with dict_cur(conn) as cur:
        cur.execute(
            "INSERT INTO issues (filename, year, month_label, ingested_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (filename, year, month_label, datetime.now(UTC)),
        )
        issue_id = cur.fetchone()["id"]
    conn.commit()

    return _segment_and_extract(
        issue_id, pdf_path, model, conn, gcs, stats,
        use_images=use_images,
    )


def _reprocess_existing_issue(
    issue_id: int,
    pdf_path: Path,
    model: str,
    conn,
    gcs: storage.Client,
    stats: dict,
    *,
    use_images: bool = True,
) -> dict:
    """Non-destructive --reprocess: keep issue + articles + prior runs,
    just re-run probes (cached by content_hash) and re-materialize."""
    tqdm.write(f"  Reprocessing (non-destructive): {pdf_path.name}")

    # Backfill issue metadata if a legacy ingest left it null.
    year, month_label = parse_issue_filename(pdf_path.name)
    with dict_cur(conn) as cur:
        cur.execute(
            """
            UPDATE issues
            SET year        = COALESCE(year,        %s),
                month_label = COALESCE(month_label, %s)
            WHERE id = %s
            """,
            (year, month_label, issue_id),
        )
    conn.commit()

    page_texts = extract_page_texts(pdf_path)
    # Page URIs are deterministic from issue_id; we don't re-render.
    page_uris = [
        f"gs://{GCS_BUCKET}/{GCS_IMAGES_PREFIX}/{issue_id}/page_{i:04d}.jpg"
        for i in range(1, len(page_texts) + 1)
    ]

    with dict_cur(conn) as cur:
        articles = _fetch_articles_for_issue(cur, issue_id)

    for article in articles:
        stats["articles"] += 1
        if article["article_type"] in NON_PROBED_ARTICLE_TYPES:
            continue
        # Backfill content_hash if it was missing on a legacy article.
        if not article["content_hash"]:
            p_start, p_end = article["page_start"], article["page_end"]
            new_hash = compute_article_hash(page_texts[p_start - 1:p_end])
            with dict_cur(conn) as cur:
                cur.execute(
                    "UPDATE articles SET content_hash=%s WHERE id=%s",
                    (new_hash, article["id"]),
                )
            conn.commit()
            article["content_hash"] = new_hash

        try:
            _run_probes_for_article(
                conn, article, page_texts,
                page_uris if use_images else None,
                model=model,
            )
            project_id, n_claims, n_quotes = materialize_from_probes(conn, article["id"])
        except Exception as exc:
            conn.rollback()
            stats.setdefault("article_failures", 0)
            stats["article_failures"] += 1
            tqdm.write(
                f"    SKIP article_id={article['id']} after error: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if project_id:
            stats["projects"] += 1
        stats["claims"] += n_claims
        stats["quotes"] += n_quotes

    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest UCD magazine PDFs into Cloud SQL")
    parser.add_argument("--pdfs", nargs="+", help="Specific PDF file(s)")
    parser.add_argument("--issues_dir", default=None, help="Directory of PDFs")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LiteLLM model (default: {DEFAULT_MODEL})")
    parser.add_argument("--reprocess", action="store_true", help="Re-ingest already-processed issues")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N issues (for validation)")
    parser.add_argument("--no-images", action="store_true",
                        help="Probe text-only (skip GCS image fetch). Cheaper smoke-test mode.")
    args = parser.parse_args()

    if args.issues_dir:
        pdf_paths = sorted(Path(args.issues_dir).glob("*.pdf"))
    elif args.pdfs:
        pdf_paths = [Path(p) for p in args.pdfs if Path(p).exists()]
    else:
        print("ERROR: Provide --issues_dir or --pdfs", file=sys.stderr)
        sys.exit(1)

    if args.limit:
        pdf_paths = pdf_paths[:args.limit]
        tqdm.write(f"Validation mode: processing {len(pdf_paths)} issue(s)")

    conn = get_conn()
    gcs = gcs_client()
    totals = {"articles": 0, "projects": 0, "claims": 0, "quotes": 0}

    with tqdm(pdf_paths, desc="Issues", unit="issue") as bar:
        for pdf_path in bar:
            bar.set_postfix_str(pdf_path.name[:40])
            stats = ingest_issue(
                pdf_path, args.model, conn, gcs,
                force=args.reprocess, use_images=not args.no_images,
            )
            for k in totals:
                totals[k] += stats[k]

    conn.close()
    print(f"\nDone: {totals}")


if __name__ == "__main__":
    main()
