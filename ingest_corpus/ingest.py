#!/usr/bin/env python3
"""
Ingestion pipeline for UCD magazine PDFs.

For each issue:
  1. Render pages to JPEG images and upload to GCS
  2. LLM pass: segment pages into articles
  3. LLM pass: extract structured data + claims + quotes per article
  4. Write everything to Cloud SQL (PostgreSQL)

Usage:
    python ingest.py --issues_dir ../issues/
    python ingest.py --pdfs ../issues/UC-D+February+2026-spreads.pdf
    python ingest.py --issues_dir ../issues/ --reprocess
    python ingest.py --issues_dir ../issues/ --limit 3   # validate first 3 issues
"""

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import litellm
import pdfplumber
import tenacity
from dotenv import load_dotenv
from google.cloud import storage
from pdf2image import convert_from_path
from PIL import Image
from tqdm import tqdm

from core.db import dict_cur, get_conn

load_dotenv()
litellm.suppress_debug_info = True

DEFAULT_MODEL = "vertex_ai/claude-sonnet-4-5@20250929"
IMAGE_DPI = 150
GCS_BUCKET = "uc-and-d-assets"
GCS_IMAGES_PREFIX = "page_images"


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


# ── LLM helpers ───────────────────────────────────────────────────────────────

@tenacity.retry(
    retry=tenacity.retry_if_exception_type((litellm.RateLimitError, litellm.APIConnectionError, litellm.InternalServerError)),
    wait=tenacity.wait_exponential(multiplier=2, min=10, max=120),
    stop=tenacity.stop_after_attempt(8),
    before_sleep=lambda rs: tqdm.write(f"  rate limit, retrying in {rs.next_action.sleep:.0f}s..."),
)
def call_llm(model: str, messages: list, max_tokens: int = 4096) -> str:
    response = litellm.completion(model=model, max_tokens=max_tokens, messages=messages)
    return response.choices[0].message.content.strip()


def parse_json_response(raw: str) -> dict | list:
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


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
        for i, (uri, text) in enumerate(zip(batch_uris, batch_texts)):
            page_num = batch_start + i + 1
            content.append({"type": "text", "text": f"\n--- Page {page_num} (extracted text) ---\n{text}"})
            content.append({"type": "text", "text": f"--- Page {page_num} (image) ---"})
            content.append(image_content(uri, gcs))

        raw = call_llm(model, [{"role": "user", "content": content}])
        try:
            all_segments.extend(parse_json_response(raw))
        except (json.JSONDecodeError, ValueError) as e:
            tqdm.write(f"  WARNING: segmentation parse error (pages {batch_start+1}-{batch_start+len(batch_uris)}): {e}")

    return all_segments


# ── Stage 2: Article extraction ────────────────────────────────────────────────

EXTRACT_PROMPT = """You are extracting structured data and content from a construction magazine article.

Each page is provided as extracted PDF text followed by the page image. The extracted text is
verbatim from the PDF — treat it as the authoritative source for all specific facts: numbers,
dollar amounts, square footages, seat counts, firm names, people names, and direct quotations.
Use the image for visual layout context only (identifying info panels, pull quotes, bylines).
Never substitute a value from the image if the extracted text gives you the same information
more clearly — the text is always more accurate.

I will show you the pages of a single article. Extract the following and return as JSON:

{
  "summary": "<2-3 sentence summary>",
  "project": {
    "name": <string or null>,
    "typology": <"K-12"|"higher_ed"|"healthcare"|"industrial"|"multifamily"|"mixed_use"|"office"|"aviation"|"hospitality"|"civic"|"religious"|"recreation"|"infrastructure"|"senior_living"|"retail"|"other"|null>,
    "location": <string or null>,
    "city": <string or null>,
    "state": <2-letter code or null>,
    "cost": <original string e.g. "$45,900,000" or null>,
    "square_footage": <original string e.g. "34,000 SF" or null>,
    "stories_levels": <string or null>,
    "delivery_method": <string or null>,
    "year_completed": <4-digit int or null>,
    "status": <"completed"|"under_construction"|"announced"|null>
  },
  "design_team": [{"role": <string>, "firm": <string>}],
  "construction_team": [{"role": <string>, "firm": <string>}],
  "owner": <string or null>,
  "owner_rep": <string or null>,
  "developer": <string or null>,
  "claims": [{"text": <string>, "type": <"stat"|"milestone"|"challenge"|"award"|"first"|"other">, "page": <int>}],
  "quotes": [{"text": <string>, "speaker_name": <string or null>, "speaker_title": <string or null>, "speaker_firm": <string or null>, "page": <int>}]
}

Rules for teams:
- Extract every firm listed in the project info panel. Use the role exactly as printed.
- If a role has multiple firms, emit one entry per firm.
- Do NOT invent roles or firms not explicitly printed.

Rules for claims:
- Notable factual assertions only: statistics, superlatives, milestones, challenges, awards, regional firsts.
- Include the page number. Ignore marketing boilerplate.

Rules for quotes:
- Verbatim pull quotes or clearly attributed direct speech only.

If this is not a project feature, return project as null and empty teams.
Return ONLY valid JSON.
"""


def extract_article(model: str, page_uris: list[str], page_texts: list[str], page_start: int, gcs: storage.Client) -> dict | None:
    content = [{"type": "text", "text": EXTRACT_PROMPT}]
    for i, (uri, text) in enumerate(zip(page_uris, page_texts)):
        page_num = page_start + i
        content.append({"type": "text", "text": f"\n--- Page {page_num} (extracted text) ---\n{text}"})
        content.append({"type": "text", "text": f"--- Page {page_num} (image) ---"})
        content.append(image_content(uri, gcs))

    raw = call_llm(model, [{"role": "user", "content": content}], max_tokens=4096)
    try:
        return parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        tqdm.write(f"  WARNING: extraction parse error (page {page_start}+): {e}")
        tqdm.write(f"  Raw: {raw[:300]}")
        return None


# ── DB writes ──────────────────────────────────────────────────────────────────

def upsert_firm(cur, raw_name: str) -> int:
    cur.execute("SELECT id FROM firms WHERE name = %s", (raw_name,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO firms (name) VALUES (%s) RETURNING id", (raw_name,))
    firm_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO firm_mentions (raw_text, canonical_id, confidence) VALUES (%s, %s, %s)",
        (raw_name, firm_id, 0.5),
    )
    return firm_id


def write_extraction(cur, article_id: int, data: dict) -> int | None:
    proj_data = data.get("project") or {}
    project_id = None

    if proj_data and proj_data.get("name"):
        def parse_int(s):
            if not s:
                return None
            digits = re.sub(r"[^\d]", "", str(s))
            return int(digits) if digits else None

        cost_str = proj_data.get("cost")
        sq_str = proj_data.get("square_footage")

        cur.execute("""
            INSERT INTO projects
                (name, typology, location, city, state, cost, cost_usd,
                 square_footage, sq_ft, stories_levels, delivery_method,
                 year_completed, status, source_article_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            proj_data.get("name"), proj_data.get("typology"),
            proj_data.get("location"), proj_data.get("city"), proj_data.get("state"),
            cost_str, parse_int(cost_str),
            sq_str, parse_int(sq_str),
            proj_data.get("stories_levels"), proj_data.get("delivery_method"),
            proj_data.get("year_completed"), proj_data.get("status"),
            article_id,
        ))
        project_id = cur.fetchone()["id"]

        if data.get("owner"):
            firm_id = upsert_firm(cur, data["owner"])
            cur.execute(
                "INSERT INTO roles (project_id, firm_id, role, team, raw_name) VALUES (%s,%s,%s,%s,%s)",
                (project_id, firm_id, "Owner", "owner", data["owner"]),
            )

        for entry in data.get("design_team") or []:
            firm_id = upsert_firm(cur, entry["firm"])
            cur.execute(
                "INSERT INTO roles (project_id, firm_id, role, team, raw_name) VALUES (%s,%s,%s,%s,%s)",
                (project_id, firm_id, entry["role"], "design", entry["firm"]),
            )

        for entry in data.get("construction_team") or []:
            firm_id = upsert_firm(cur, entry["firm"])
            cur.execute(
                "INSERT INTO roles (project_id, firm_id, role, team, raw_name) VALUES (%s,%s,%s,%s,%s)",
                (project_id, firm_id, entry["role"], "construction", entry["firm"]),
            )

    for claim in data.get("claims") or []:
        cur.execute(
            "INSERT INTO claims (article_id, project_id, text, type, page) VALUES (%s,%s,%s,%s,%s)",
            (article_id, project_id, claim["text"], claim.get("type"), claim.get("page")),
        )

    for q in data.get("quotes") or []:
        cur.execute("""
            INSERT INTO quotes
                (article_id, project_id, speaker_name, speaker_title, speaker_firm, text, page)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (article_id, project_id, q.get("speaker_name"), q.get("speaker_title"),
              q.get("speaker_firm"), q["text"], q.get("page")))

    cur.execute(
        "UPDATE articles SET primary_project_id=%s, summary=%s WHERE id=%s",
        (project_id, data.get("summary"), article_id),
    )

    return project_id


# ── Main pipeline ──────────────────────────────────────────────────────────────

def ingest_issue(pdf_path: Path, model: str, conn, gcs: storage.Client, force: bool = False) -> dict:
    filename = pdf_path.name
    stats = {"articles": 0, "projects": 0, "claims": 0, "quotes": 0}

    with dict_cur(conn) as cur:
        cur.execute("SELECT id FROM issues WHERE filename=%s", (filename,))
        existing = cur.fetchone()

    if existing and not force:
        tqdm.write(f"  SKIP {filename} (already ingested)")
        return stats

    if existing and force:
        with dict_cur(conn) as cur:
            # Null out source_article_id on projects before cascade-deleting articles
            cur.execute("""
                UPDATE projects SET source_article_id = NULL
                WHERE source_article_id IN (
                    SELECT id FROM articles WHERE issue_id = %s
                )
            """, (existing["id"],))
            cur.execute("DELETE FROM issues WHERE id=%s", (existing["id"],))
        conn.commit()

    with dict_cur(conn) as cur:
        cur.execute(
            "INSERT INTO issues (filename, ingested_at) VALUES (%s, %s) RETURNING id",
            (filename, datetime.now(timezone.utc)),
        )
        issue_id = cur.fetchone()["id"]
    conn.commit()

    tqdm.write(f"  Rendering + uploading pages: {filename}")
    page_uris = render_and_upload(pdf_path, issue_id, gcs)
    page_texts = extract_page_texts(pdf_path)

    with dict_cur(conn) as cur:
        cur.execute("UPDATE issues SET page_count=%s WHERE id=%s", (len(page_uris), issue_id))
    conn.commit()

    tqdm.write(f"  Segmenting {len(page_uris)} pages...")
    segments = segment_issue(model, page_uris, page_texts, gcs)
    tqdm.write(f"  Found {len(segments)} segments")

    for seg in segments:
        p_start = seg.get("page_start", 1)
        p_end = seg.get("page_end", p_start)

        with dict_cur(conn) as cur:
            cur.execute("""
                INSERT INTO articles
                    (issue_id, page_start, page_end, title, author, article_type, ingested_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (issue_id, p_start, p_end, seg.get("title"), seg.get("author"),
                  seg.get("type"), datetime.now(timezone.utc)))
            article_id = cur.fetchone()["id"]
        conn.commit()
        stats["articles"] += 1

        if seg.get("type") in ("advertisement", "other"):
            continue

        tqdm.write(f"    Extracting pages {p_start}-{p_end}: {(seg.get('title') or '(untitled)')[:60]}")
        article_uris = page_uris[p_start - 1:p_end]
        article_texts = page_texts[p_start - 1:p_end]
        data = extract_article(model, article_uris, article_texts, p_start, gcs)

        if data:
            with dict_cur(conn) as cur:
                project_id = write_extraction(cur, article_id, data)
            conn.commit()
            if project_id:
                stats["projects"] += 1
            stats["claims"] += len(data.get("claims") or [])
            stats["quotes"] += len(data.get("quotes") or [])

    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest UCD magazine PDFs into Cloud SQL")
    parser.add_argument("--pdfs", nargs="+", help="Specific PDF file(s)")
    parser.add_argument("--issues_dir", default=None, help="Directory of PDFs")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LiteLLM model (default: {DEFAULT_MODEL})")
    parser.add_argument("--reprocess", action="store_true", help="Re-ingest already-processed issues")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N issues (for validation)")
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
            stats = ingest_issue(pdf_path, args.model, conn, gcs, force=args.reprocess)
            for k in totals:
                totals[k] += stats[k]

    conn.close()
    print(f"\nDone: {totals}")


if __name__ == "__main__":
    main()
