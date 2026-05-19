#!/usr/bin/env python3
"""
Probe runner.

For every (probe in REGISTRY × article in scope), check whether a
probe_run already exists for the article's current content_hash at the
probe's current version. If not, render the page texts (and images,
when GCS is configured), call the LLM, and write a probe_run row.

Caching:
  - probe_runs is unique on
    (probe_id, article_id, probe_version, content_hash).
  - A re-run with the same inputs is a no-op (skipped without LLM call).
  - Bumping a probe's version, or recomputing a different content_hash
    on an article (e.g. re-OCR), causes the next run to do real work.

Usage:
    python -m core.probes.runner                     # run all active probes
                                                     # against every article
    python -m core.probes.runner --probe claims_v1
    python -m core.probes.runner --article-ids 12,17
    python -m core.probes.runner --limit 50          # cap LLM calls
    python -m core.probes.runner --no-images         # text-only (cheaper)
"""

import hashlib
import json
import sys

from tqdm import tqdm

from core.db import dict_cur
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response
from core.probes import REGISTRY


def compute_content_hash(page_texts: list[str]) -> str:
    """Stable hash over the article's page texts."""
    h = hashlib.sha256()
    for t in page_texts:
        h.update((t or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# ── Article fetch + text assembly ────────────────────────────────────────────

def fetch_articles(
    cur,
    article_ids: list[int] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Pull articles with their page texts joined in.

    Articles do not store their own page text; we reconstruct the
    content hash from the issues table's PDFs at run time. For the
    initial cut of the runner we expect content_hash to already be
    populated on articles by ingest_corpus/. If it's missing, we skip
    the article with a warning — the alternative (re-rendering PDFs
    here) duplicates ingest_corpus and breaks the separation.
    """
    sql = """
        SELECT a.id, a.issue_id, a.page_start, a.page_end,
               a.title, a.article_type, a.content_hash
        FROM articles a
        WHERE a.article_type IS NULL
           OR a.article_type NOT IN ('advertisement', 'column', 'other')
    """
    params: list = []
    if article_ids:
        sql += " AND a.id = ANY(%s)"
        params.append(article_ids)
    sql += " ORDER BY a.id"
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    cur.execute(sql, params)
    return cur.fetchall()


def get_probe_id(cur, name: str, version: int) -> int | None:
    cur.execute(
        "SELECT id FROM probes WHERE name=%s AND version=%s",
        (name, version),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def existing_run(
    cur,
    probe_id: int,
    article_id: int,
    probe_version: int,
    content_hash: str | None,
) -> bool:
    cur.execute(
        """
        SELECT 1 FROM probe_runs
        WHERE probe_id = %s
          AND article_id = %s
          AND probe_version = %s
          AND COALESCE(content_hash, '') = COALESCE(%s, '')
        LIMIT 1
        """,
        (probe_id, article_id, probe_version, content_hash),
    )
    return cur.fetchone() is not None


def write_run(
    cur,
    probe_id: int,
    article_id: int,
    probe_version: int,
    model: str,
    content_hash: str | None,
    output: dict,
) -> None:
    cur.execute(
        """
        INSERT INTO probe_runs
            (probe_id, article_id, probe_version, model, content_hash,
             output_json, ran_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """,
        (probe_id, article_id, probe_version, model, content_hash,
         json.dumps(output)),
    )


# ── Per-article execution ────────────────────────────────────────────────────

def render_message_content(
    prompt: str,
    page_texts: list[str],
    page_start: int,
    image_uris: list[str] | None,
    *,
    article_title: str | None = None,
) -> list[dict]:
    """Build the LiteLLM `content` array. If image_uris is None,
    the article is sent text-only (cheaper, lower fidelity).

    `article_title` is injected immediately after the probe prompt
    so the model can disambiguate which article it's probing when
    the page text contains content for adjacent articles (a real
    issue in Best-of-Year award round-ups; see probe v4 docstrings).
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    if article_title:
        content.append({
            "type": "text",
            "text": f"\n--- Article title: {article_title} ---",
        })
    for i, text in enumerate(page_texts):
        page_num = page_start + i
        content.append({
            "type": "text",
            "text": f"\n--- Page {page_num} (extracted text) ---\n{text}",
        })
    if image_uris:
        # Lazy import — avoids the GCS dep in pure text mode.
        from core.probes._images import image_content_blocks
        content.extend(image_content_blocks(image_uris, page_start))
    return content


def run_probe_for_article(
    conn,
    spec,
    article: dict,
    page_texts: list[str],
    *,
    model: str,
    image_uris: list[str] | None = None,
) -> bool:
    """Run a single probe against a single article. Returns True if a
    run was performed (False = cache hit, no LLM call)."""
    content_hash = article.get("content_hash") or compute_content_hash(page_texts)

    with dict_cur(conn) as cur:
        probe_id = get_probe_id(cur, spec.name, spec.version)
        if probe_id is None:
            raise RuntimeError(
                f"Probe {spec.name} v{spec.version} not in DB. "
                "Run: python -m core.probes.seed"
            )
        if existing_run(cur, probe_id, article["id"], spec.version, content_hash):
            return False

    messages = [{
        "role": "user",
        "content": render_message_content(
            spec.prompt, page_texts, article["page_start"], image_uris,
            article_title=article.get("title"),
        ),
    }]
    # 16384 leaves headroom for Gemini's internal thinking tokens on
    # long-feature articles. Audit caught Pro hitting 8192 mid-quote
    # on the densest articles. Anthropic caps silently at its own
    # max output (8192 default) so this is harmless there.
    raw = call_llm(model, messages, max_tokens=16384)
    try:
        output = parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        tqdm.write(f"  parse error on article {article['id']} / {spec.name}: {e}")
        output = {"_error": str(e), "_raw": raw[:1000]}

    with dict_cur(conn) as cur:
        write_run(cur, probe_id, article["id"], spec.version,
                  model, content_hash, output)
    conn.commit()
    return True


def run_probes(
    conn,
    *,
    probe_names: list[str] | None = None,
    article_ids: list[int] | None = None,
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    page_texts_loader=None,
    image_uris_loader=None,
) -> dict:
    """Run every active probe (or the subset named) against every
    in-scope article. The two loader callbacks let the caller decide
    where page texts and image URIs come from — typically
    ingest_corpus.ingest provides them."""
    probes = (
        [REGISTRY[n] for n in probe_names]
        if probe_names
        else list(REGISTRY.values())
    )

    with dict_cur(conn) as cur:
        articles = fetch_articles(cur, article_ids=article_ids, limit=limit)

    stats = {"runs": 0, "cache_hits": 0, "errors": 0}
    for article in tqdm(articles, desc="Articles"):
        if page_texts_loader is None:
            tqdm.write(
                f"  no page_texts_loader: skipping article {article['id']} "
                "(provide one to actually run probes)"
            )
            stats["errors"] += 1
            continue
        page_texts = page_texts_loader(article)
        image_uris = image_uris_loader(article) if image_uris_loader else None
        for spec in probes:
            try:
                ran = run_probe_for_article(
                    conn, spec, article, page_texts,
                    model=model, image_uris=image_uris,
                )
                stats["runs" if ran else "cache_hits"] += 1
            except Exception as e:
                tqdm.write(f"  error on {spec.name} / article {article['id']}: {e}")
                stats["errors"] += 1
    return stats


def main():
    # The CLI is intentionally a stub: the runner needs a page-text
    # loader (and optionally an image-uris loader) supplied by the
    # caller. ingest_corpus/ingest.py is the production caller; tests
    # and ad-hoc scripts can build their own loaders and call
    # run_probes() directly.
    print(
        "Runner CLI requires page-text and image loaders. Import\n"
        "  from core.probes import run_probes\n"
        "and call it from a script that knows how to fetch page text\n"
        "for each article (ingest_corpus exposes the helpers).",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
