#!/usr/bin/env python3
"""
Embeddings sweep.

Populates `articles.embedding`, `projects.embedding`, `claims.embedding`
in batches for any rows where the column is NULL. Idempotent: re-running
only embeds rows added since the last run.

Default model is OpenAI `text-embedding-3-small` (1536 dims, matches
the `vector(1536)` schema). The model is overridable per-call so a
future migration to Vertex's textembedding-gecko or a Voyage model
doesn't require code changes here — it does require a schema change
to match the new dim.

Usage:
    python -m core.embeddings.embed                    # all three tables
    python -m core.embeddings.embed --tables articles  # subset
    python -m core.embeddings.embed --limit 100        # cap per table
    python -m core.embeddings.embed --redo             # re-embed everything

Requires OPENAI_API_KEY (LiteLLM passes it through). The vertex /
anthropic gateways don't currently expose an embeddings API, so we
route to OpenAI directly even when chat completions go elsewhere.
"""

import argparse
import json
import sys

import litellm
import tenacity
from tqdm import tqdm

from core.db import dict_cur, get_conn

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_BATCH_SIZE = 100  # OpenAI accepts up to 2048 inputs per call.


@tenacity.retry(
    retry=tenacity.retry_if_exception_type((
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.InternalServerError,
    )),
    wait=tenacity.wait_exponential(multiplier=2, min=10, max=120),
    stop=tenacity.stop_after_attempt(8),
    before_sleep=lambda rs: tqdm.write(
        f"  embedding rate limit, retrying in {rs.next_action.sleep:.0f}s..."
    ),
)
def _embed_batch(model: str, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input."""
    if not texts:
        return []
    resp = litellm.embedding(model=model, input=texts)
    return [d["embedding"] for d in resp.data]


# ── Per-table embedding inputs ────────────────────────────────────────────────
#
# The chat agent's `semantic_search` tool runs cosine NN against these
# vectors, so the text we embed has to actually describe *what the row
# is*, not just identify it. Earlier versions embedded just `name +
# typology + city/state` for projects — that left the Delta Sky Club
# losing rank to I-15 on the query "airport terminal aviation
# infrastructure" because both share the typology=infrastructure
# token but only Delta's *article content* mentions airports.
#
# Each embedding input now joins through to the row's connected
# content (article summaries, top claims, team firm names) so the
# vector captures the entity's full story, not just its metadata.
# Total length is capped per row so a project with 30 articles doesn't
# blow past OpenAI's 8K-token-per-input limit.

_PROJECT_TEXT_MAX_CHARS = 3000
_ARTICLE_TEXT_MAX_CHARS = 2400
_CLAIM_TEXT_MAX_CHARS   = 600
_QUOTE_TEXT_MAX_CHARS   = 800
_FIRM_TEXT_MAX_CHARS    = 800


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _project_text(row: dict) -> str:
    """Rich project embedding input: metadata + linked article
    summaries + top claims + top quotes + team firm names. Encodes
    what the project actually IS, not just its file-card facts."""
    location_parts = [row.get("city"), row.get("state"), row.get("county")]
    where = " ".join(filter(None, location_parts))
    parts = [
        row.get("name") or "",
        row.get("typology") or "",
        where,
        row.get("location") or "",
        row.get("status") or "",
        f"Year completed {row['year_completed']}" if row.get("year_completed") else "",
        f"Cost {row['cost']}" if row.get("cost") else "",
        row.get("article_summaries") or "",
        row.get("top_claims") or "",
        row.get("top_quotes") or "",
        f"Team: {row['firms']}" if row.get("firms") else "",
    ]
    text = " | ".join(p for p in parts if p).strip()
    return _truncate(text, _PROJECT_TEXT_MAX_CHARS)


def _article_text(row: dict) -> str:
    """Rich article embedding input: title + author + summary + linked
    project name(s) + top claims + top quotes. Caller filters to
    project_feature + null-type articles so ads/columns don't pollute."""
    parts = [
        row.get("title") or "",
        f"By {row['author']}" if row.get("author") else "",
        row.get("summary") or "",
        f"Project: {row['project_names']}" if row.get("project_names") else "",
        row.get("top_claims") or "",
        row.get("top_quotes") or "",
    ]
    text = " | ".join(p for p in parts if p).strip()
    return _truncate(text, _ARTICLE_TEXT_MAX_CHARS)


def _claim_text(row: dict) -> str:
    """Enriched claim text: project + type prefix gives the embedding
    enough context to distinguish 'cost overrun on Delta Sky Club'
    from 'cost overrun on Alpine Aqueduct' (would otherwise collapse
    to similar vectors). Bare text remains the dominant signal."""
    prefix_parts = []
    if row.get("project_name"):
        prefix_parts.append(row["project_name"])
    if row.get("type"):
        prefix_parts.append(row["type"])
    prefix = f"[{', '.join(prefix_parts)}] " if prefix_parts else ""
    text = (prefix + (row.get("text") or "")).strip()
    return _truncate(text, _CLAIM_TEXT_MAX_CHARS)


def _quote_text(row: dict) -> str:
    """Quote embedding input: text + speaker (name, title, firm) +
    project context. Lets the agent search by quote content, by
    speaker, by firm, or by project."""
    speaker_parts = []
    if row.get("speaker_name"):
        speaker_parts.append(row["speaker_name"])
    if row.get("speaker_title"):
        speaker_parts.append(row["speaker_title"])
    if row.get("speaker_firm"):
        speaker_parts.append(f"at {row['speaker_firm']}")
    speaker = " — " + ", ".join(speaker_parts) if speaker_parts else ""
    project = f" (project: {row['project_name']})" if row.get("project_name") else ""
    text = f"{(row.get('text') or '').strip()}{speaker}{project}"
    return _truncate(text, _QUOTE_TEXT_MAX_CHARS)


def _firm_text(row: dict) -> str:
    """Firm embedding input: name + aliases + firm_type + role labels.
    Encodes the firm's industry profile so queries like 'architects
    specializing in adaptive reuse' can find the right firms by their
    project history, not just their name."""
    aliases = row.get("aliases") or []
    parts = [
        row.get("name") or "",
        f"Type: {row['firm_type']}" if row.get("firm_type") and row["firm_type"] != "unknown" else "",
        f"Also known as: {', '.join(aliases)}" if aliases else "",
        f"Roles: {row['roles_played']}" if row.get("roles_played") else "",
    ]
    text = " | ".join(p for p in parts if p).strip()
    return _truncate(text, _FIRM_TEXT_MAX_CHARS)


# ── Generic per-table sweep ──────────────────────────────────────────────────

def _run_embedding_pass(
    conn,
    *,
    table: str,
    sql: str,
    text_fn,
    model: str,
    batch_size: int,
) -> int:
    """Generic sweep: SELECT rows via `sql`, render each through
    `text_fn`, batch-embed, UPDATE. Caller controls the SQL (including
    WHERE / LIMIT) so each table can fetch its own rich joined data."""
    with dict_cur(conn) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    if not rows:
        return 0

    n_updated = 0
    for i in tqdm(range(0, len(rows), batch_size), desc=table):
        batch = rows[i:i + batch_size]
        prepared = [(r["id"], text_fn(r)) for r in batch]
        prepared = [(rid, t) for rid, t in prepared if t]
        if not prepared:
            continue

        ids   = [rid for rid, _ in prepared]
        texts = [t   for _, t   in prepared]
        vectors = _embed_batch(model, texts)

        with dict_cur(conn) as cur:
            for rid, vec in zip(ids, vectors, strict=True):
                cur.execute(
                    f"UPDATE {table} SET embedding = %s::vector WHERE id = %s",
                    (vec, rid),
                )
        conn.commit()
        n_updated += len(prepared)

    return n_updated


# ── Rich SQL per table ───────────────────────────────────────────────────────

def _projects_sql(redo: bool, limit: int | None) -> str:
    """Joined query: project metadata + linked article summaries +
    top claims + top quotes + team firm names. The quote inclusion
    is new in this revision — see plan §3.A.1: voices about the
    project are real semantic signal."""
    where = "TRUE" if redo else "p.embedding IS NULL"
    sql = f"""
        SELECT
            p.id, p.name, p.typology, p.city, p.state, p.county,
            p.location, p.status, p.year_completed, p.cost,
            (
                SELECT string_agg(LEFT(a.summary, 700), ' | ' ORDER BY a.id)
                FROM article_projects ap
                JOIN articles a ON a.id = ap.article_id
                WHERE ap.project_id = p.id AND a.summary IS NOT NULL
            ) AS article_summaries,
            (
                SELECT string_agg(t.text, ' | ' ORDER BY length(t.text) DESC)
                FROM (
                    SELECT text FROM claims
                    WHERE project_id = p.id
                    ORDER BY length(text) DESC LIMIT 8
                ) t
            ) AS top_claims,
            (
                SELECT string_agg(LEFT(q.text, 200), ' | ' ORDER BY length(q.text) DESC)
                FROM (
                    SELECT text FROM quotes
                    WHERE project_id = p.id
                    ORDER BY length(text) DESC LIMIT 4
                ) q
            ) AS top_quotes,
            (
                SELECT string_agg(DISTINCT f.name, ', ' ORDER BY f.name)
                FROM roles r JOIN firms f ON f.id = r.firm_id
                WHERE r.project_id = p.id
            ) AS firms
        FROM projects p
        WHERE {where}
        ORDER BY p.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def _articles_sql(redo: bool, limit: int | None) -> str:
    """Joined query: article metadata + linked project name(s) + top
    claims + top quotes. Filters out advertisement/column/other so
    ads don't pollute semantic search. Apology summaries from
    overlap-bug articles are filtered out via the summary heuristic;
    they'd embed as 'no information about...' noise otherwise."""
    where_clauses = [
        "(a.article_type IS NULL OR a.article_type = 'project_feature')",
        "(a.summary IS NULL OR a.summary NOT ILIKE '%pages do not contain%')",
        "(a.summary IS NULL OR a.summary NOT ILIKE '%no information about%')",
    ]
    if not redo:
        where_clauses.append("a.embedding IS NULL")
    where = " AND ".join(where_clauses)
    sql = f"""
        SELECT
            a.id, a.title, a.author, a.summary,
            (
                SELECT string_agg(p.name, ' | ' ORDER BY p.id)
                FROM article_projects ap JOIN projects p ON p.id = ap.project_id
                WHERE ap.article_id = a.id
            ) AS project_names,
            (
                SELECT string_agg(t.text, ' | ' ORDER BY length(t.text) DESC)
                FROM (
                    SELECT text FROM claims
                    WHERE article_id = a.id
                    ORDER BY length(text) DESC LIMIT 5
                ) t
            ) AS top_claims,
            (
                SELECT string_agg(LEFT(q.text, 200), ' | ' ORDER BY length(q.text) DESC)
                FROM (
                    SELECT text FROM quotes
                    WHERE article_id = a.id
                    ORDER BY length(text) DESC LIMIT 4
                ) q
            ) AS top_quotes
        FROM articles a
        WHERE {where}
        ORDER BY a.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def _claims_sql(redo: bool, limit: int | None) -> str:
    """Each claim with its project name + type, so the embedding has
    enough context to distinguish claims about different projects."""
    where = "TRUE" if redo else "c.embedding IS NULL"
    sql = f"""
        SELECT c.id, c.text, c.type,
               (SELECT name FROM projects WHERE id = c.project_id) AS project_name
        FROM claims c
        WHERE {where}
        ORDER BY c.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def _quotes_sql(redo: bool, limit: int | None) -> str:
    """Each quote with speaker fields + linked project name."""
    where = "TRUE" if redo else "q.embedding IS NULL"
    sql = f"""
        SELECT q.id, q.text, q.speaker_name, q.speaker_title, q.speaker_firm,
               (SELECT name FROM projects WHERE id = q.project_id) AS project_name
        FROM quotes q
        WHERE {where}
        ORDER BY q.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def _firms_sql(redo: bool, limit: int | None) -> str:
    """Each firm with aliases + firm_type + concatenated distinct role
    labels played across all projects."""
    where = "TRUE" if redo else "f.embedding IS NULL"
    sql = f"""
        SELECT f.id, f.name, f.firm_type, f.aliases,
               (
                   SELECT string_agg(DISTINCT r.role, ', ' ORDER BY r.role)
                   FROM roles r WHERE r.firm_id = f.id
               ) AS roles_played
        FROM firms f
        WHERE {where}
        ORDER BY f.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


# ── Public API ───────────────────────────────────────────────────────────────

def embed_articles(conn, *, model=DEFAULT_EMBEDDING_MODEL,
                   batch_size=DEFAULT_BATCH_SIZE,
                   limit=None, redo=False) -> int:
    """Embed `project_feature` (and null-type) articles only. Drops
    embeddings on ads/columns/other in `redo` mode so a re-run cleans
    out the pre-rich-embedding pollution from semantic search."""
    if redo:
        with dict_cur(conn) as cur:
            cur.execute(
                "UPDATE articles SET embedding = NULL "
                "WHERE article_type IN ('advertisement','column','other')"
            )
        conn.commit()
    return _run_embedding_pass(
        conn, table="articles", sql=_articles_sql(redo, limit),
        text_fn=_article_text, model=model, batch_size=batch_size,
    )


def embed_projects(conn, *, model=DEFAULT_EMBEDDING_MODEL,
                   batch_size=DEFAULT_BATCH_SIZE,
                   limit=None, redo=False) -> int:
    return _run_embedding_pass(
        conn, table="projects", sql=_projects_sql(redo, limit),
        text_fn=_project_text, model=model, batch_size=batch_size,
    )


def embed_claims(conn, *, model=DEFAULT_EMBEDDING_MODEL,
                 batch_size=DEFAULT_BATCH_SIZE,
                 limit=None, redo=False) -> int:
    return _run_embedding_pass(
        conn, table="claims", sql=_claims_sql(redo, limit),
        text_fn=_claim_text, model=model, batch_size=batch_size,
    )


def embed_quotes(conn, *, model=DEFAULT_EMBEDDING_MODEL,
                 batch_size=DEFAULT_BATCH_SIZE,
                 limit=None, redo=False) -> int:
    return _run_embedding_pass(
        conn, table="quotes", sql=_quotes_sql(redo, limit),
        text_fn=_quote_text, model=model, batch_size=batch_size,
    )


def embed_firms(conn, *, model=DEFAULT_EMBEDDING_MODEL,
                batch_size=DEFAULT_BATCH_SIZE,
                limit=None, redo=False) -> int:
    return _run_embedding_pass(
        conn, table="firms", sql=_firms_sql(redo, limit),
        text_fn=_firm_text, model=model, batch_size=batch_size,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

_TABLE_FNS = {
    "articles": embed_articles,
    "projects": embed_projects,
    "claims":   embed_claims,
    "quotes":   embed_quotes,
    "firms":    embed_firms,
}


def main():
    p = argparse.ArgumentParser(description="Populate embedding columns.")
    p.add_argument("--tables", nargs="+", choices=list(_TABLE_FNS),
                   default=list(_TABLE_FNS))
    p.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--redo", action="store_true")
    args = p.parse_args()

    conn = get_conn()
    try:
        results = {}
        for tbl in args.tables:
            results[tbl] = _TABLE_FNS[tbl](
                conn, model=args.model, batch_size=args.batch_size,
                limit=args.limit, redo=args.redo,
            )
    finally:
        conn.close()
    print(json.dumps(results, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
