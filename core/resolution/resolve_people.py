#!/usr/bin/env python3
"""
Person entity resolution.

Mirror of resolve_firms but keyed on (name, firm_id): two people with
the same name at different firms are distinct until proven otherwise.

Public API:
  upsert_person(cur, raw_name, raw_title, raw_firm, firm_id) -> int | None
      Ingest-time helper. Resolves a quote's speaker tuple to a
      canonical people row deterministically; creates one if no match.
      Always writes a person_mentions row for downstream resolution.
      Returns person_id, or None if raw_name is empty.

  python -m core.resolution.resolve_people  [--limit N] [--redo]
      Standalone pass over unresolved person_mentions, with LLM
      tiebreaker for trgm-medium candidates.
"""

import argparse
import json
import sys

from tqdm import tqdm

from core.db import dict_cur, get_conn
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response
from core.resolution.normalize import normalize_person_name

EXACT_MATCH_CONF  = 1.00
TRGM_HIGH_CONF    = 0.92
TRGM_CANDIDATE    = 0.55
MIN_RESOLVED_CONF = 0.80
LLM_BATCH_SIZE    = 25


LLM_PROMPT = """You are resolving construction-industry person-name variants
to canonical people records. People are scoped per firm — two people with
the same name at different firms are distinct.

You will receive:
  - A list of UNKNOWN raw person mentions (name, title, firm).
  - A list of CANDIDATE canonical people that share name fragments and
    are at the same firm.

For each unknown, decide:
  - "match" — refers to one of the candidates. Return canonical_id +
              confidence in [0, 1].
  - "new"   — distinct person. Return a clean canonical name (proper
              case, full form if obvious).
  - "skip"  — not a real person ("a spokesperson", "officials"). conf 0.

Return ONLY a JSON array, one object per unknown, in input order:
[
  {"raw_name": "<echo>", "decision": "match"|"new"|"skip",
   "canonical_id": <int or null>, "canonical_name": <string or null>,
   "confidence": <float>}
]
"""


# ── Deterministic match (used at ingest + by the standalone resolver) ────────

def deterministic_match(
    cur,
    raw_name: str,
    firm_id: int | None,
) -> tuple[int | None, float, list[dict]]:
    """Return (person_id, confidence, candidates_for_llm). High-confidence
    hits leave candidates empty; ambiguous cases return the trgm top-k
    so an LLM tiebreaker can decide.

    All lookups are scoped to `firm_id` (NULL-safe via IS NOT DISTINCT FROM)
    so identical names at different firms stay distinct."""
    norm = normalize_person_name(raw_name)
    if not norm:
        return None, 0.0, []

    # 1. Exact name match within firm.
    cur.execute(
        "SELECT id, name FROM people "
        "WHERE LOWER(name) = LOWER(%s) AND firm_id IS NOT DISTINCT FROM %s",
        (raw_name, firm_id),
    )
    row = cur.fetchone()
    if row:
        return row["id"], EXACT_MATCH_CONF, []

    # 2. Normalized form matches an existing person within firm.
    cur.execute(
        "SELECT id, name FROM people WHERE firm_id IS NOT DISTINCT FROM %s",
        (firm_id,),
    )
    for p in cur.fetchall():
        if normalize_person_name(p["name"]) == norm:
            return p["id"], EXACT_MATCH_CONF, []

    # 3. Alias match within firm.
    cur.execute(
        """
        SELECT p.id, p.name
        FROM people p, jsonb_array_elements_text(p.aliases) AS a
        WHERE LOWER(a) = LOWER(%s)
          AND p.firm_id IS NOT DISTINCT FROM %s
        LIMIT 1
        """,
        (raw_name, firm_id),
    )
    row = cur.fetchone()
    if row:
        return row["id"], EXACT_MATCH_CONF, []

    # 4. Trgm similarity within firm.
    cur.execute(
        """
        SELECT id, name, similarity(name, %s) AS sim
        FROM people
        WHERE name %% %s AND firm_id IS NOT DISTINCT FROM %s
        ORDER BY sim DESC
        LIMIT 5
        """,
        (raw_name, raw_name, firm_id),
    )
    candidates = cur.fetchall()
    if candidates and candidates[0]["sim"] >= TRGM_HIGH_CONF:
        top = candidates[0]
        return top["id"], float(top["sim"]), []

    candidates = [c for c in candidates if c["sim"] >= TRGM_CANDIDATE]
    return None, 0.0, candidates


# ── Ingest-time helper ───────────────────────────────────────────────────────

def upsert_person(
    cur,
    raw_name: str | None,
    raw_title: str | None,
    raw_firm: str | None,
    firm_id: int | None,
) -> int | None:
    """Resolve `raw_name` to a canonical people row within `firm_id`'s
    scope, or create a new row. Always writes a person_mentions entry
    for the standalone resolver to clean up later. Returns person_id,
    or None if raw_name is empty.

    `raw_firm` is the original speaker_firm string from the quote; it
    goes into person_mentions for audit. `firm_id` is the canonical id
    the caller already resolved (`upsert_firm` in ingest_corpus)."""
    if not raw_name or not raw_name.strip():
        return None

    person_id, confidence, _candidates = deterministic_match(cur, raw_name, firm_id)
    if person_id is None:
        # Race-safe insert against the (name, COALESCE(firm_id, 0))
        # unique index. On conflict, fall back to looking up the row
        # that beat us to it.
        cur.execute(
            """
            INSERT INTO people (name, title, firm_id) VALUES (%s, %s, %s)
            ON CONFLICT (name, COALESCE(firm_id, 0)) DO NOTHING
            RETURNING id
            """,
            (raw_name, raw_title, firm_id),
        )
        result = cur.fetchone()
        if result:
            person_id = result["id"]
        else:
            cur.execute(
                "SELECT id FROM people "
                "WHERE name = %s AND COALESCE(firm_id, 0) = COALESCE(%s, 0)",
                (raw_name, firm_id),
            )
            person_id = cur.fetchone()["id"]
        confidence = None  # signals "needs the LLM resolver"

    cur.execute(
        """
        INSERT INTO person_mentions
            (raw_name, raw_title, raw_firm, canonical_id, confidence)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (raw_name, raw_title, raw_firm, person_id, confidence),
    )
    return person_id


# ── Standalone resolver pass ─────────────────────────────────────────────────

def fetch_unresolved(cur, limit: int | None, redo: bool) -> list[dict]:
    where = "TRUE" if redo else (
        f"(canonical_id IS NULL OR confidence IS NULL OR confidence < {MIN_RESOLVED_CONF}) "
        f"AND corrected = FALSE"
    )
    sql = f"""
        SELECT id, raw_name, raw_title, raw_firm, canonical_id
        FROM person_mentions
        WHERE {where}
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return cur.fetchall()


def _resolve_firm_id_for_mention(cur, raw_firm: str | None) -> int | None:
    """Find an existing firm matching `raw_firm`. Mirrors the
    ingest-time deterministic match but never creates a new firm —
    the resolver shouldn't conjure firms from person_mentions alone."""
    if not raw_firm:
        return None
    cur.execute(
        "SELECT id FROM firms WHERE LOWER(name) = LOWER(%s) LIMIT 1",
        (raw_firm,),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def _llm_decide_batch(
    model: str,
    batch: list[tuple[dict, list[dict]]],
) -> list[dict]:
    unknowns = [
        {"raw_name": m["raw_name"], "title": m.get("raw_title"),
         "firm": m.get("raw_firm")}
        for m, _ in batch
    ]
    cand_ids: dict[int, dict] = {}
    for _, cands in batch:
        for c in cands:
            cand_ids[c["id"]] = {
                "id": c["id"], "name": c["name"],
                "aliases": c.get("aliases") or [],
            }
    payload = (
        f"UNKNOWN people:\n{json.dumps(unknowns, indent=2)}\n\n"
        f"CANDIDATE canonical people:\n{json.dumps(list(cand_ids.values()), indent=2)}\n"
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


def _write_resolution(cur, mention_id: int, person_id: int, confidence: float) -> None:
    cur.execute(
        "UPDATE person_mentions SET canonical_id=%s, confidence=%s WHERE id=%s",
        (person_id, confidence, mention_id),
    )


def _flush_llm_batch(conn, model, batch, stats):
    decisions = _llm_decide_batch(model, batch)
    by_raw = {d.get("raw_name"): d for d in decisions}
    with dict_cur(conn) as cur:
        for m, _ in batch:
            d = by_raw.get(m["raw_name"])
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
                _write_resolution(cur, m["id"], cid, conf)
                stats["resolved_llm"] += 1
            elif decision == "new" and d.get("canonical_name"):
                # Keep the existing placeholder person if there is one;
                # the resolver doesn't conjure new people, just confirms
                # them. Bump confidence to capture the LLM's vote.
                if m.get("canonical_id"):
                    _write_resolution(cur, m["id"], m["canonical_id"], conf)
                    stats["confirmed_new"] += 1
                else:
                    stats["skipped"] += 1
            else:
                stats["skipped"] += 1
    conn.commit()


def resolve_people(
    conn,
    *,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    redo: bool = False,
) -> dict:
    stats = {"resolved_deterministic": 0, "resolved_llm": 0,
             "confirmed_new": 0, "skipped": 0, "ambiguous": 0}

    with dict_cur(conn) as cur:
        mentions = fetch_unresolved(cur, limit, redo)
    if not mentions:
        return stats

    pending: list[tuple[dict, list[dict]]] = []

    for m in tqdm(mentions, desc="People"):
        with dict_cur(conn) as cur:
            firm_id = _resolve_firm_id_for_mention(cur, m.get("raw_firm"))
            person_id, conf, candidates = deterministic_match(
                cur, m["raw_name"], firm_id,
            )
        if person_id is not None:
            with dict_cur(conn) as cur:
                _write_resolution(cur, m["id"], person_id, conf)
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


def main():
    p = argparse.ArgumentParser(description="Resolve person_mentions to canonical people.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--redo", action="store_true")
    args = p.parse_args()

    conn = get_conn()
    try:
        stats = resolve_people(conn, model=args.model, limit=args.limit, redo=args.redo)
    finally:
        conn.close()
    print(json.dumps(stats, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
