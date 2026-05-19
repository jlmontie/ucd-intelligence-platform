#!/usr/bin/env python3
"""
Consolidation passes that run *after* mention→canonical resolution to
clean up duplicate canonical entities and child rows.

Mirrors the `merge_projects` / `consolidate` pattern in
resolve_projects.py:
  - `merge_firms(conn, winner, loser)` — re-points every firm_id FK
    (roles, people, firm_mentions) with collision handling on
    natural-key UNIQUEs, then deletes the loser. Inherits loser's
    name as an alias on the winner.
  - `consolidate_firms_by_parenthetical(conn)` — deterministic: finds
    firms whose names differ only by a trailing parenthetical
    (`"Flynn Companies"` ↔ `"Flynn Companies (patching)"`) and merges
    the parenthesized variant into the bare one.
  - `consolidate_roles(conn)` — strips parentheticals from `roles.role`
    and dedupes within (project_id, firm_id, role_canon, team). The
    parenthetical content is preserved on the surviving row's
    `raw_name` for now; `scope` column work lands in a later
    migration.

All operations are idempotent. CLI usage:
    python -m core.resolution.consolidate firms --apply
    python -m core.resolution.consolidate roles --apply
    python -m core.resolution.consolidate firms                # dry-run
"""

import argparse
import json
import re
import sys

from tqdm import tqdm

from core.db import dict_cur, get_conn
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response

# ── merge_firms primitive ────────────────────────────────────────────────────

def merge_firms(conn, winner_id: int, loser_id: int) -> dict:
    """Re-point every firm_id reference from loser to winner, then
    delete the loser firm row. Returns per-table counts of rows
    re-pointed. Loser's name is captured as an alias on the winner.

    Handles UNIQUE-constraint collisions on `roles.uniq_roles_natural_key`
    (project_id, firm_id, role, team) and `idx_people_name_firm`
    (name, COALESCE(firm_id, 0)) by deleting loser rows that would
    collide with existing winner rows before the bulk UPDATE.

    Raises ValueError if winner == loser or either id is missing."""
    if winner_id == loser_id:
        raise ValueError("winner and loser must be distinct")

    counts: dict[str, int] = {}
    with dict_cur(conn) as cur:
        cur.execute(
            "SELECT id, name, aliases FROM firms WHERE id = ANY(%s)",
            ([winner_id, loser_id],),
        )
        rows = {r["id"]: r for r in cur.fetchall()}
        for fid in (winner_id, loser_id):
            if fid not in rows:
                raise ValueError(f"firm id {fid} not found")
        winner_row = rows[winner_id]
        loser_row  = rows[loser_id]

        # roles — UNIQUE (project_id, firm_id, role, team)
        cur.execute(
            """
            DELETE FROM roles
            WHERE firm_id = %s
              AND (project_id, role, team) IN (
                  SELECT project_id, role, team FROM roles WHERE firm_id = %s
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE roles SET firm_id = %s WHERE firm_id = %s",
            (winner_id, loser_id),
        )
        counts["roles"] = cur.rowcount

        # people — UNIQUE (name, COALESCE(firm_id, 0)).
        # Collision-doomed loser-people (same name as a winner-person at
        # the winner firm) must be deleted before bulk-updating firm_id.
        # But they may be referenced by quotes.speaker_person_id and
        # person_mentions.canonical_id, neither of which has ON DELETE,
        # so we first re-point those refs to the matching winner-people.
        cur.execute(
            """
            SELECT l.id AS loser_pid, w.id AS winner_pid
            FROM people l
            JOIN people w ON w.firm_id = %s AND w.name = l.name
            WHERE l.firm_id = %s
            """,
            (winner_id, loser_id),
        )
        collisions = cur.fetchall()
        for c in collisions:
            cur.execute(
                "UPDATE quotes SET speaker_person_id = %s WHERE speaker_person_id = %s",
                (c["winner_pid"], c["loser_pid"]),
            )
            cur.execute(
                "UPDATE person_mentions SET canonical_id = %s WHERE canonical_id = %s",
                (c["winner_pid"], c["loser_pid"]),
            )
        cur.execute(
            """
            DELETE FROM people
            WHERE firm_id = %s
              AND name IN (SELECT name FROM people WHERE firm_id = %s)
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE people SET firm_id = %s WHERE firm_id = %s",
            (winner_id, loser_id),
        )
        counts["people"] = cur.rowcount

        # firm_mentions — no uniqueness; plain re-point
        cur.execute(
            "UPDATE firm_mentions SET canonical_id = %s WHERE canonical_id = %s",
            (winner_id, loser_id),
        )
        counts["firm_mentions"] = cur.rowcount

        # Capture the loser's name as an alias on the winner (plus any
        # aliases it already had). Dedupe and drop the winner's own name.
        combined: set[str] = set(winner_row["aliases"] or [])
        combined.update(loser_row["aliases"] or [])
        combined.add(loser_row["name"])
        combined.discard(winner_row["name"])
        cur.execute(
            "UPDATE firms SET aliases = %s::jsonb WHERE id = %s",
            (json.dumps(sorted(combined)), winner_id),
        )

        cur.execute("DELETE FROM firms WHERE id = %s", (loser_id,))

    conn.commit()
    return counts


# ── Parenthetical-stripping helpers ──────────────────────────────────────────

_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")
_PAREN_ANY  = re.compile(r"\s*\([^)]*\)\s*")


def _strip_trailing_paren(s: str) -> str:
    """Drop a trailing (parenthesized) qualifier from a name/role string.
    Used to collapse 'Flynn Companies (patching)' → 'Flynn Companies'
    while leaving 'A (B) Architects' alone — internal parentheticals
    rarely indicate the same parenthetical-placement bug."""
    return _PAREN_TAIL.sub("", s).strip()


def _strip_all_paren(s: str) -> str:
    """Aggressive: drop every parenthesized sub-expression. Used as a
    fallback canonicalizer for role strings where parentheticals can
    appear mid-string too ('Glazing/Curtain Wall (interior)')."""
    return _PAREN_ANY.sub(" ", s).strip()


# ── Firms: parenthetical-only duplicates ─────────────────────────────────────

def consolidate_firms_by_parenthetical(conn, *, apply: bool) -> dict:
    """Find firm pairs `(A, A (qualifier))` and merge the parenthesized
    variant into the bare one. Deterministic — no LLM call needed.

    Plan from tonight's review: catches the 262 paren-tagged firms
    that resolver couldn't merge because pg_trgm similarity was below
    0.92. Most of these have a bare-name twin in the firms table
    already; merging is unambiguous."""
    stats = {"clusters_seen": 0, "merges": 0, "rows_repointed": 0}

    with dict_cur(conn) as cur:
        cur.execute("SELECT id, name FROM firms ORDER BY id")
        firms = cur.fetchall()

    # Group by stripped canonical name. Each non-singleton group is
    # a merge candidate; the bare-name row (or lowest-id) wins.
    by_canon: dict[str, list[dict]] = {}
    for f in firms:
        canon = _strip_trailing_paren(f["name"]).lower()
        if not canon:
            continue
        by_canon.setdefault(canon, []).append(f)

    for _canon, group in by_canon.items():
        if len(group) < 2:
            continue
        stats["clusters_seen"] += 1
        # Winner = the firm whose name has no trailing paren if one
        # exists, else lowest id.
        bare = [f for f in group if _strip_trailing_paren(f["name"]) == f["name"]]
        winner = (bare[0] if bare else min(group, key=lambda f: f["id"]))
        for loser in group:
            if loser["id"] == winner["id"]:
                continue
            print(json.dumps({
                "action": "merge_firm",
                "winner": {"id": winner["id"], "name": winner["name"]},
                "loser":  {"id": loser["id"],  "name": loser["name"]},
                "dry_run": not apply,
            }))
            if apply:
                c = merge_firms(conn, winner["id"], loser["id"])
                stats["merges"] += 1
                stats["rows_repointed"] += sum(c.values())

    return stats


# ── Roles: parenthetical-only duplicates ─────────────────────────────────────

def consolidate_roles(conn, *, apply: bool) -> dict:
    """Dedupe roles where one row has the bare role string
    ('Roofing') and another has the same role with a parenthetical
    qualifier ('Roofing (patching)'). The bare form wins; the
    parenthesized form is dropped.

    Skipped: clusters where every row has its own distinct
    parenthetical (e.g. one firm on one project recorded as both
    'Mechanical (HVAC)' AND 'Mechanical (Plumbing)'). Those are
    legitimately different trade packages and merging them would lose
    information. Only collapses cases where the bare form is the
    canonical authority and the parenthesized variants are
    supplemental scope notes on the same role.

    Parenthetical scope detail is lost here; a follow-up migration
    adds `roles.scope` to preserve it on new ingests. Pre-migration
    cleanup is auditable from `probe_runs.output_json`.
    """
    stats = {"clusters_seen": 0, "merges": 0, "rows_deleted": 0,
             "clusters_skipped_no_bare_form": 0}

    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT id, project_id, firm_id, role, team, raw_name, confidence
            FROM roles
            ORDER BY id
        """)
        roles = cur.fetchall()

    by_canon: dict[tuple, list[dict]] = {}
    for r in roles:
        canon = _strip_all_paren(r["role"]).lower()
        key = (r["project_id"], r["firm_id"], r["team"], canon)
        by_canon.setdefault(key, []).append(r)

    losers_to_delete: list[int] = []
    for key, group in by_canon.items():
        if len(group) < 2:
            continue
        stats["clusters_seen"] += 1
        bare = [r for r in group if "(" not in r["role"]]
        if not bare:
            # No bare-form authority. The parenthesized variants are
            # distinct scopes; leave them alone.
            stats["clusters_skipped_no_bare_form"] += 1
            continue
        winner = min(bare, key=lambda r: r["id"])
        for loser in group:
            if loser["id"] == winner["id"]:
                continue
            losers_to_delete.append(loser["id"])
            print(json.dumps({
                "action": "delete_role",
                "winner_id": winner["id"],
                "loser_id":  loser["id"],
                "project_id": key[0],
                "firm_id":   key[1],
                "winner_role": winner["role"],
                "loser_role":  loser["role"],
                "dry_run": not apply,
            }))
        stats["merges"] += 1

    if apply and losers_to_delete:
        with dict_cur(conn) as cur:
            cur.execute("DELETE FROM roles WHERE id = ANY(%s)", (losers_to_delete,))
        conn.commit()
        stats["rows_deleted"] = len(losers_to_delete)

    return stats


# ── Firms: fuzzy duplicates (LLM tiebreaker) ─────────────────────────────────

LLM_PAIR_PROMPT = """You are deciding whether pairs of construction-industry
firm names refer to the SAME business entity.

For each pair, decide:
- "same"     — same firm. Typical reasons: typos, plurals, possessives,
               "&" vs "and", word reorderings, legal-suffix differences
               (Inc / LLC / Corp), abbreviation expansion (e.g. "BC&A"
               vs "Bowen Collins & Associates"), trailing scope notes
               that don't change identity.
- "distinct" — different firms that just share name tokens. Different
               trade categories ("Smith Electric" vs "Smith Mechanical"),
               different geographic specifiers when they imply separate
               operating companies, or clearly different entities.
- "skip"     — uncertain; not enough info to call.

Return ONLY a JSON array, one object per input pair, in input order:
[{"a_id": <int>, "b_id": <int>, "decision": "same"|"distinct"|"skip"}]
"""


# 15 pairs/batch keeps the output JSON well under Gemini's thinking-budget
# truncation point. The earlier 25-pair attempt got truncated to 4 entries
# because Gemini 2.5's internal reasoning consumed most of max_tokens=4096
# before reaching the structured-output phase.
LLM_PAIR_BATCH_SIZE = 15


def _llm_decide_pairs(model: str, pairs: list[dict]) -> list[dict]:
    """Send a batch of candidate pairs to the LLM, return one decision per pair."""
    payload = "Pairs:\n" + json.dumps(
        [{"a_id": p["a_id"], "a_name": p["a_name"],
          "b_id": p["b_id"], "b_name": p["b_name"]} for p in pairs],
        indent=2,
    )
    raw = call_llm(model, [
        {"role": "user", "content": [
            {"type": "text", "text": LLM_PAIR_PROMPT},
            {"type": "text", "text": payload},
        ]},
    ], max_tokens=16384)
    try:
        return parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        tqdm.write(f"  LLM parse error: {e}; raw={raw[:200]!r}")
        return []


def consolidate_firms_fuzzy(
    conn,
    *,
    apply: bool,
    model: str = DEFAULT_MODEL,
    sim_floor: float = 0.55,
    sim_ceiling: float = 0.92,
    limit: int | None = None,
) -> dict:
    """Find firm pairs at pg_trgm similarity in [sim_floor, sim_ceiling)
    and ask an LLM to decide whether each pair is the same firm.
    Merges 'same' verdicts via `merge_firms`.

    The deterministic resolver auto-merges pairs >= 0.92; pairs < 0.55
    are too noisy to surface. This pass cleans up the middle band the
    resolver intentionally avoided without LLM help.

    Idempotent: re-running after merges have applied surfaces no
    pairs because the merged firms are gone from the firms table.
    Tracks a union-find map so that within a single run, a chain of
    "same" decisions (A=B, B=C) merges into a single canonical firm
    rather than crashing on the second merge."""
    stats = {"pairs_found": 0, "same": 0, "distinct": 0,
             "skipped": 0, "merge_failed": 0, "rows_repointed": 0}

    with dict_cur(conn) as cur:
        cur.execute(
            """
            SELECT a.id AS a_id, a.name AS a_name,
                   b.id AS b_id, b.name AS b_name,
                   similarity(a.name, b.name) AS sim
            FROM firms a
            JOIN firms b ON a.id < b.id AND a.name %% b.name
            WHERE similarity(a.name, b.name) >= %s
              AND similarity(a.name, b.name) <  %s
            ORDER BY sim DESC
            """,
            (sim_floor, sim_ceiling),
        )
        pairs = cur.fetchall()

    if limit:
        pairs = pairs[:limit]
    stats["pairs_found"] = len(pairs)
    if not pairs:
        return stats

    merged_into: dict[int, int] = {}

    def _resolve(fid: int) -> int:
        while fid in merged_into:
            fid = merged_into[fid]
        return fid

    for i in tqdm(range(0, len(pairs), LLM_PAIR_BATCH_SIZE),
                  desc="firm-pair batches"):
        batch = pairs[i:i + LLM_PAIR_BATCH_SIZE]
        # Drop pairs whose endpoints are already merged together
        # (transitive merges within this run).
        usable = []
        for p in batch:
            a = _resolve(p["a_id"])
            b = _resolve(p["b_id"])
            if a == b:
                continue
            usable.append(p)
        if not usable:
            continue

        decisions = _llm_decide_pairs(model, usable)
        by_pair = {(d.get("a_id"), d.get("b_id")): d for d in decisions
                   if "a_id" in d and "b_id" in d}

        for p in usable:
            d = by_pair.get((p["a_id"], p["b_id"]))
            decision = (d or {}).get("decision", "skip")
            if decision == "same":
                stats["same"] += 1
                if not apply:
                    print(json.dumps({
                        "action": "merge_firm_fuzzy",
                        "a": {"id": p["a_id"], "name": p["a_name"]},
                        "b": {"id": p["b_id"], "name": p["b_name"]},
                        "sim": float(p["sim"]),
                        "dry_run": True,
                    }))
                    continue
                a = _resolve(p["a_id"])
                b = _resolve(p["b_id"])
                if a == b:
                    continue
                winner, loser = (a, b) if a < b else (b, a)
                try:
                    counts = merge_firms(conn, winner, loser)
                    merged_into[loser] = winner
                    stats["rows_repointed"] += sum(counts.values())
                    print(json.dumps({
                        "action": "merge_firm_fuzzy",
                        "winner": {"id": winner, "name": p["a_name"] if p["a_id"] == winner else p["b_name"]},
                        "loser":  {"id": loser,  "name": p["b_name"] if p["b_id"] == loser  else p["a_name"]},
                        "sim": float(p["sim"]),
                        "dry_run": False,
                    }))
                except (ValueError, Exception) as e:
                    stats["merge_failed"] += 1
                    tqdm.write(f"  merge failed for ({winner}, {loser}): {e}")
            elif decision == "distinct":
                stats["distinct"] += 1
            else:
                stats["skipped"] += 1

    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Consolidate duplicate firms and roles.")
    p.add_argument("target",
                   choices=("firms", "firms-fuzzy", "roles"),
                   help="Which consolidation pass to run.")
    p.add_argument("--apply", action="store_true",
                   help="Actually perform the merges. Without this flag, dry-run only.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="LLM for firms-fuzzy tiebreaker (default: core.llm.DEFAULT_MODEL).")
    p.add_argument("--limit", type=int, default=None,
                   help="firms-fuzzy: cap the number of candidate pairs evaluated.")
    args = p.parse_args()

    conn = get_conn()
    try:
        if args.target == "firms":
            stats = consolidate_firms_by_parenthetical(conn, apply=args.apply)
        elif args.target == "firms-fuzzy":
            stats = consolidate_firms_fuzzy(
                conn, apply=args.apply, model=args.model, limit=args.limit,
            )
        else:
            stats = consolidate_roles(conn, apply=args.apply)
    finally:
        conn.close()

    print(json.dumps(stats, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
