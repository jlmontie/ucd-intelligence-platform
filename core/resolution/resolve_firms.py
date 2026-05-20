#!/usr/bin/env python3
"""
Firm entity resolution.

Replaces the exact-match upsert in ingest_corpus/ingest.py:254. Reads
unresolved firm_mentions, attempts a deterministic match against
existing firms via normalized-name equality and pg_trgm similarity,
then falls back to an LLM tiebreaker for ambiguous batches.

Idempotent: rows already resolved with confidence ≥ MIN_RESOLVED_CONF
are skipped. Set --redo to re-resolve everything.

Usage:
    python -m core.resolution.resolve_firms              # resolve all unresolved
    python -m core.resolution.resolve_firms --limit 100  # cap LLM calls
    python -m core.resolution.resolve_firms --redo       # re-resolve everything
"""

import argparse
import json
import sys

from tqdm import tqdm

from core.db import dict_cur, get_conn
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response
from core.resolution.normalize import normalize_firm_name

# Confidence thresholds.
EXACT_MATCH_CONF = 1.00          # normalized name == normalized name
TRGM_HIGH_CONF   = 0.92          # pg_trgm similarity that we trust without LLM
TRGM_CANDIDATE   = 0.55          # similarity floor for considering as candidate
MIN_RESOLVED_CONF = 0.80         # below this is treated as unresolved on re-runs
LLM_BATCH_SIZE   = 25


LLM_PROMPT = """You are resolving construction-firm name variants to canonical
firm records.

You will receive:
  - A list of UNKNOWN raw firm name strings extracted from magazine articles.
  - A list of CANDIDATE canonical firms (id + name + known aliases) that share
    name fragments with one or more unknowns.

For each unknown, decide:
  - "match"  — the unknown refers to one of the candidate firms. Return the
               canonical id and a confidence in [0, 1].
  - "new"    — the unknown is a distinct firm not in the candidate list.
               Return a clean canonical name (proper case, full legal name
               if obvious, otherwise the most common form).
  - "skip"   — the raw text is not actually a firm name (e.g. "TBD",
               "various subcontractors"). Use confidence 0.

Return ONLY a JSON array, one object per unknown, in input order:
[
  {"raw": "<echo input>", "decision": "match"|"new"|"skip",
   "canonical_id": <int or null>, "canonical_name": <string or null>,
   "confidence": <float>}
]
"""


def fetch_unresolved(cur, limit: int | None, redo: bool) -> list[dict]:
    where = "TRUE" if redo else (
        f"(canonical_id IS NULL OR confidence IS NULL OR confidence < {MIN_RESOLVED_CONF}) "
        f"AND corrected = FALSE"
    )
    sql = f"""
        SELECT id, raw_text
        FROM firm_mentions
        WHERE {where}
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return cur.fetchall()


def find_trgm_candidates(cur, raw: str, k: int = 5) -> list[dict]:
    """Top-k existing firms by pg_trgm similarity to `raw`."""
    cur.execute(
        """
        SELECT id, name, aliases, similarity(name, %s) AS sim
        FROM firms
        WHERE name %% %s
        ORDER BY sim DESC
        LIMIT %s
        """,
        (raw, raw, k),
    )
    return cur.fetchall()


def deterministic_match(cur, raw: str) -> tuple[int | None, float, list[dict]]:
    """Return (firm_id, confidence, candidates_for_llm).

    If we can decide the match deterministically, candidates is empty.
    Otherwise candidates is the trigram top-k for the LLM to consider.

    Steps 1 (exact case-insensitive raw match), 3 (alias match), and
    4 (trgm) work on the raw input directly. Step 2 (normalized form
    match) requires a non-empty normalized form — names like
    "Architects, Inc." normalize to empty because every token is a
    legal/industry suffix in `_FIRM_SUFFIXES`. We skip step 2 in that
    case rather than short-circuiting the whole function, because step
    1 still has a real chance to catch the duplicate.
    """
    norm = normalize_firm_name(raw)

    # 1. Exact case-insensitive raw match — runs regardless of norm.
    cur.execute("SELECT id, name FROM firms WHERE LOWER(name) = LOWER(%s)", (raw,))
    row = cur.fetchone()
    if row:
        return row["id"], EXACT_MATCH_CONF, []

    # 2. Normalized form match — skipped when norm is empty (would
    #    otherwise produce false positives matching every other
    #    all-suffix firm name).
    if norm:
        cur.execute("SELECT id, name FROM firms")
        for f in cur.fetchall():
            if normalize_firm_name(f["name"]) == norm:
                return f["id"], EXACT_MATCH_CONF, []

    # 3. Normalized form matches a known alias.
    cur.execute(
        """
        SELECT id, name
        FROM firms, jsonb_array_elements_text(aliases) AS a
        WHERE LOWER(a) = LOWER(%s)
        LIMIT 1
        """,
        (raw,),
    )
    row = cur.fetchone()
    if row:
        return row["id"], EXACT_MATCH_CONF, []

    # 4. Trigram similarity. High enough → trust it; otherwise pass to LLM.
    candidates = find_trgm_candidates(cur, raw)
    if candidates and candidates[0]["sim"] >= TRGM_HIGH_CONF:
        top = candidates[0]
        return top["id"], float(top["sim"]), []

    candidates = [c for c in candidates if c["sim"] >= TRGM_CANDIDATE]
    return None, 0.0, candidates


def write_resolution(
    cur,
    mention_id: int,
    raw: str,
    firm_id: int,
    confidence: float,
) -> None:
    cur.execute(
        "UPDATE firm_mentions SET canonical_id=%s, confidence=%s WHERE id=%s",
        (firm_id, confidence, mention_id),
    )
    # Add the raw form as an alias if not already present and it differs
    # from the canonical name.
    cur.execute("SELECT name, aliases FROM firms WHERE id=%s", (firm_id,))
    row = cur.fetchone()
    canonical_name = row["name"]
    aliases = row["aliases"] or []
    if raw != canonical_name and raw not in aliases:
        aliases.append(raw)
        cur.execute(
            "UPDATE firms SET aliases=%s WHERE id=%s",
            (json.dumps(aliases), firm_id),
        )


def create_firm(cur, canonical_name: str) -> int:
    cur.execute(
        "SELECT id FROM firms WHERE LOWER(name) = LOWER(%s)",
        (canonical_name,),
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(
        "INSERT INTO firms (name) VALUES (%s) RETURNING id",
        (canonical_name,),
    )
    return cur.fetchone()["id"]


def llm_batch_resolve(
    model: str,
    batch: list[tuple[dict, list[dict]]],
) -> list[dict]:
    unknowns = [{"raw": m["raw_text"]} for m, _ in batch]
    cand_ids: dict[int, dict] = {}
    for _, cands in batch:
        for c in cands:
            cand_ids[c["id"]] = {
                "id": c["id"],
                "name": c["name"],
                "aliases": c.get("aliases") or [],
            }

    payload = (
        f"UNKNOWN raw firm names:\n{json.dumps(unknowns, indent=2)}\n\n"
        f"CANDIDATE canonical firms:\n{json.dumps(list(cand_ids.values()), indent=2)}\n"
    )
    raw = call_llm(model, [
        {"role": "user", "content": [
            {"type": "text", "text": LLM_PROMPT},
            {"type": "text", "text": payload},
        ]},
    ], max_tokens=2048)
    try:
        return parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        tqdm.write(f"  LLM parse error: {e}; raw={raw[:200]!r}")
        return []


def resolve_firms(
    conn,
    *,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    redo: bool = False,
) -> dict:
    """Resolve unresolved firm_mentions. Returns counts."""
    stats = {"resolved_deterministic": 0, "resolved_llm": 0,
             "created": 0, "skipped": 0, "ambiguous": 0}

    with dict_cur(conn) as cur:
        mentions = fetch_unresolved(cur, limit, redo)

    if not mentions:
        return stats

    pending: list[tuple[dict, list[dict]]] = []

    for m in tqdm(mentions, desc="Firms"):
        with dict_cur(conn) as cur:
            firm_id, conf, candidates = deterministic_match(cur, m["raw_text"])
        if firm_id is not None:
            with dict_cur(conn) as cur:
                write_resolution(cur, m["id"], m["raw_text"], firm_id, conf)
            conn.commit()
            stats["resolved_deterministic"] += 1
            continue
        pending.append((m, candidates))
        if len(pending) >= LLM_BATCH_SIZE:
            _flush_llm_batch(conn, model, pending, stats)
            pending = []

    if pending:
        _flush_llm_batch(conn, model, pending, stats)

    return stats


def _flush_llm_batch(
    conn,
    model: str,
    batch: list[tuple[dict, list[dict]]],
    stats: dict,
) -> None:
    decisions = llm_batch_resolve(model, batch)
    by_raw = {d.get("raw"): d for d in decisions}

    with dict_cur(conn) as cur:
        for m, _ in batch:
            d = by_raw.get(m["raw_text"])
            if not d:
                stats["ambiguous"] += 1
                continue
            decision = d.get("decision")
            try:
                conf = float(d.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if decision == "match" and d.get("canonical_id"):
                try:
                    cid = int(d["canonical_id"])
                except (TypeError, ValueError):
                    stats["skipped"] += 1
                    continue
                write_resolution(cur, m["id"], m["raw_text"], cid, conf)
                stats["resolved_llm"] += 1
            elif decision == "new" and d.get("canonical_name"):
                firm_id = create_firm(cur, d["canonical_name"].strip())
                write_resolution(cur, m["id"], m["raw_text"], firm_id, conf)
                stats["created"] += 1
            else:
                stats["skipped"] += 1
    conn.commit()


def main():
    p = argparse.ArgumentParser(description="Resolve firm_mentions to canonical firms.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--redo", action="store_true",
                   help="Re-resolve mentions that already have a high-confidence match.")
    args = p.parse_args()

    conn = get_conn()
    try:
        stats = resolve_firms(conn, model=args.model, limit=args.limit, redo=args.redo)
    finally:
        conn.close()
    print(json.dumps(stats, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
