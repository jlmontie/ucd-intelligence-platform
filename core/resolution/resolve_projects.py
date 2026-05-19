#!/usr/bin/env python3
"""
Project entity resolution.

Two entry points:

  resolve_project(conn, candidate)
      Used by ingest_public/ scrapers. Takes a structured candidate
      record (name, location, cost, year, source_type, source_ref) and
      either matches it to an existing projects row or inserts a new
      one. Idempotent: replays of the same candidate produce no new
      rows. Returns (project_id, was_created, confidence).

  python -m core.resolution.resolve_projects --consolidate
      One-shot pass over the existing projects table to flag and
      optionally merge corpus duplicates (same project covered in two
      articles). --apply performs the merge; without it, candidate
      clusters are printed for review.

Match scoring uses normalized name similarity (pg_trgm), location
agreement, cost-range overlap, and year proximity. Single-candidate
high-confidence matches are auto-applied; multi-candidate or
mid-confidence matches go to the LLM tiebreaker.
"""

import argparse
import json
import sys

from tqdm import tqdm

from core.db import dict_cur, get_conn
from core.llm import DEFAULT_MODEL, call_llm, parse_json_response
from core.resolution.normalize import normalize_project_name

# Match thresholds.
NAME_SIM_HARD     = 0.92    # auto-match if name sim above this and location agrees
NAME_SIM_FLOOR    = 0.45    # below this, not a candidate at all
COST_TOLERANCE    = 0.30    # ±30% of cost considered "same range"
YEAR_TOLERANCE    = 2       # ±2 years
LLM_DECIDE_FLOOR  = 0.55    # send to LLM only if best score above this

LLM_PROMPT = """You are reconciling a CANDIDATE construction project record
against EXISTING projects that share name fragments and/or geography.

Decide whether the candidate refers to one of the existing projects
("match") or is a distinct project ("new").

Return ONLY a JSON object:
{
  "decision": "match" | "new",
  "matched_project_id": <int or null>,
  "confidence": <float in [0,1]>,
  "reason": "<one short sentence>"
}

Two records describe the same project when name, location, and either
cost or year align. A near-name with conflicting location or a
year/cost gap larger than typical project drift is "new".
"""


# ── Matching ─────────────────────────────────────────────────────────────────

def _cost_overlap(a: int | None, b: int | None) -> bool | None:
    if not a or not b:
        return None
    lo = min(a, b)
    hi = max(a, b)
    return (hi - lo) / hi <= COST_TOLERANCE


def _year_match(a: int | None, b: int | None) -> bool | None:
    if not a or not b:
        return None
    return abs(a - b) <= YEAR_TOLERANCE


def _location_match(c: dict, p: dict) -> bool | None:
    """Location agrees if city or county or (state + nearby coords) align."""
    city_a, city_b = (c.get("city") or "").lower(), (p.get("city") or "").lower()
    if city_a and city_b:
        return city_a == city_b
    cnty_a, cnty_b = (c.get("county") or "").lower(), (p.get("county") or "").lower()
    if cnty_a and cnty_b:
        return cnty_a == cnty_b
    state_a, state_b = (c.get("state") or "").upper(), (p.get("state") or "").upper()
    if state_a and state_b:
        return state_a == state_b
    return None


def _score(c: dict, p: dict, name_sim: float) -> float:
    """Combined score in [0, 1]. Name similarity dominates; soft signals nudge."""
    score = name_sim * 0.7
    loc = _location_match(c, p)
    if loc is True:
        score += 0.15
    elif loc is False:
        score -= 0.20
    cost = _cost_overlap(
        c.get("estimated_cost_usd") or c.get("cost_usd"),
        p.get("estimated_cost_usd") or p.get("cost_usd"),
    )
    if cost is True:
        score += 0.05
    elif cost is False:
        score -= 0.10
    yr = _year_match(c.get("year_completed"), p.get("year_completed"))
    if yr is True:
        score += 0.05
    elif yr is False:
        score -= 0.10
    return max(0.0, min(1.0, score))


def find_candidates(cur, candidate: dict, k: int = 8) -> list[dict]:
    name = candidate.get("name") or ""
    norm = normalize_project_name(name)
    if not norm:
        return []
    cur.execute(
        """
        SELECT id, name, typology, city, state, county,
               cost_usd, estimated_cost_usd, year_completed, source,
               similarity(name, %s) AS name_sim
        FROM projects
        WHERE name %% %s
        ORDER BY name_sim DESC
        LIMIT %s
        """,
        (name, name, k),
    )
    rows = cur.fetchall()
    return [r for r in rows if r["name_sim"] >= NAME_SIM_FLOOR]


# ── Provenance write-back ────────────────────────────────────────────────────

def _record_source(cur, project_id: int, candidate: dict, confidence: float) -> None:
    """Idempotent: ON CONFLICT advances last_seen instead of inserting."""
    src_type = candidate.get("source_type")
    src_ref = candidate.get("source_ref")
    if not src_type or not src_ref:
        return
    cur.execute(
        """
        INSERT INTO project_sources
            (project_id, source_type, source_ref, confidence)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, source_type, source_ref)
        DO UPDATE SET last_seen = NOW(),
                      confidence = GREATEST(project_sources.confidence, EXCLUDED.confidence)
        """,
        (project_id, src_type, src_ref, confidence),
    )


def _insert_project(cur, candidate: dict) -> int:
    cur.execute(
        """
        INSERT INTO projects (
            name, typology, location, city, state, county,
            lat, lng, cost, cost_usd, estimated_cost_usd,
            square_footage, sq_ft, stories_levels, delivery_method,
            year_completed, status, phase, source
        ) VALUES (
            %(name)s, %(typology)s, %(location)s, %(city)s, %(state)s, %(county)s,
            %(lat)s, %(lng)s, %(cost)s, %(cost_usd)s, %(estimated_cost_usd)s,
            %(square_footage)s, %(sq_ft)s, %(stories_levels)s, %(delivery_method)s,
            %(year_completed)s, %(status)s, %(phase)s, %(source)s
        )
        RETURNING id
        """,
        {
            "name": candidate.get("name"),
            "typology": candidate.get("typology"),
            "location": candidate.get("location"),
            "city": candidate.get("city"),
            "state": candidate.get("state"),
            "county": candidate.get("county"),
            "lat": candidate.get("lat"),
            "lng": candidate.get("lng"),
            "cost": candidate.get("cost"),
            "cost_usd": candidate.get("cost_usd"),
            "estimated_cost_usd": candidate.get("estimated_cost_usd"),
            "square_footage": candidate.get("square_footage"),
            "sq_ft": candidate.get("sq_ft"),
            "stories_levels": candidate.get("stories_levels"),
            "delivery_method": candidate.get("delivery_method"),
            "year_completed": candidate.get("year_completed"),
            "status": candidate.get("status"),
            "phase": candidate.get("phase"),
            "source": candidate.get("source", "public_data"),
        },
    )
    return cur.fetchone()["id"]


# ── Public entry point ───────────────────────────────────────────────────────

def resolve_project(
    conn,
    candidate: dict,
    *,
    model: str = DEFAULT_MODEL,
    use_llm: bool = True,
) -> tuple[int, bool, float]:
    """Match `candidate` to an existing project or create a new one.

    `candidate` is a dict with the same column names as `projects`,
    plus `source_type` and `source_ref` for provenance. Idempotent
    against the (source_type, source_ref) pair.

    Returns (project_id, was_created, confidence).
    """
    src_type = candidate.get("source_type")
    src_ref = candidate.get("source_ref")

    # Idempotency short-circuit: this exact source already points at a project.
    if src_type and src_ref:
        with dict_cur(conn) as cur:
            cur.execute(
                """
                SELECT project_id, confidence FROM project_sources
                WHERE source_type = %s AND source_ref = %s
                LIMIT 1
                """,
                (src_type, src_ref),
            )
            existing = cur.fetchone()
        if existing:
            return existing["project_id"], False, float(existing["confidence"])

    with dict_cur(conn) as cur:
        candidates = find_candidates(cur, candidate)

    scored = [(p, _score(candidate, p, float(p["name_sim"]))) for p in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)

    if scored and scored[0][1] >= NAME_SIM_HARD:
        top_p, top_s = scored[0]
        # Auto-match only if name sim is also dominant.
        if scored[0][0]["name_sim"] >= NAME_SIM_HARD and (
            len(scored) < 2 or scored[1][1] < top_s - 0.15
        ):
            with dict_cur(conn) as cur:
                _record_source(cur, top_p["id"], candidate, top_s)
            conn.commit()
            return top_p["id"], False, top_s

    decided_pid = None
    decided_conf = 0.0

    if use_llm and scored and scored[0][1] >= LLM_DECIDE_FLOOR:
        decided_pid, decided_conf = _llm_decide(model, candidate, scored[:5])

    if decided_pid is not None:
        with dict_cur(conn) as cur:
            _record_source(cur, decided_pid, candidate, decided_conf)
        conn.commit()
        return decided_pid, False, decided_conf

    # No match. Create a new canonical project and record provenance.
    with dict_cur(conn) as cur:
        new_pid = _insert_project(cur, candidate)
        _record_source(cur, new_pid, candidate, 1.0)
    conn.commit()
    return new_pid, True, 1.0


def _llm_decide(
    model: str,
    candidate: dict,
    scored: list[tuple[dict, float]],
) -> tuple[int | None, float]:
    payload = {
        "candidate": {k: candidate.get(k) for k in (
            "name", "typology", "city", "state", "county",
            "cost_usd", "estimated_cost_usd", "year_completed",
        )},
        "existing": [
            {
                "id": p["id"], "name": p["name"], "typology": p["typology"],
                "city": p["city"], "state": p["state"], "county": p["county"],
                "cost_usd": p["cost_usd"], "estimated_cost_usd": p["estimated_cost_usd"],
                "year_completed": p["year_completed"], "source": p["source"],
                "preliminary_score": round(s, 3),
            }
            for p, s in scored
        ],
    }
    raw = call_llm(model, [
        {"role": "user", "content": [
            {"type": "text", "text": LLM_PROMPT},
            {"type": "text", "text": json.dumps(payload, indent=2)},
        ]},
    ], max_tokens=512)
    try:
        decision = parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        tqdm.write(f"  LLM parse error: {e}")
        return None, 0.0

    if decision.get("decision") == "match" and decision.get("matched_project_id"):
        # Defensive cast: model occasionally returns "id_42" or a
        # float-as-string instead of a clean integer. Falling through
        # to None forces the caller to create a new project rather
        # than crashing the corpus ingest mid-stream.
        try:
            pid = int(decision["matched_project_id"])
            conf = float(decision.get("confidence") or 0)
        except (TypeError, ValueError):
            tqdm.write(
                f"  LLM tiebreaker returned non-numeric ids: "
                f"matched_project_id={decision.get('matched_project_id')!r} "
                f"confidence={decision.get('confidence')!r}"
            )
            return None, 0.0
        return pid, conf
    return None, 0.0


# ── Merge primitive ──────────────────────────────────────────────────────────

def merge_projects(conn, winner_id: int, loser_id: int) -> dict:
    """Re-point every reference from `loser_id` to `winner_id`, then delete
    the loser row. Returns per-table counts of rows re-pointed.

    Every child table has a natural-key UNIQUE constraint (plan §2.6).
    The merge follows a uniform pattern per table: delete loser rows
    whose natural-key already exists on the winner side, then plain
    UPDATE to re-point the survivors. Without the pre-delete step the
    UPDATE would violate the unique constraint.

    Commits once at the end. Raises ValueError if winner == loser or if
    either id does not exist.
    """
    if winner_id == loser_id:
        raise ValueError("winner and loser must be distinct")

    counts: dict[str, int] = {}
    with dict_cur(conn) as cur:
        cur.execute(
            "SELECT id FROM projects WHERE id = ANY(%s)",
            ([winner_id, loser_id],),
        )
        found = {r["id"] for r in cur.fetchall()}
        for pid in (winner_id, loser_id):
            if pid not in found:
                raise ValueError(f"project id {pid} not found")

        # article_projects — PK (article_id, project_id).
        cur.execute(
            """
            DELETE FROM article_projects
            WHERE project_id = %s
              AND article_id IN (
                  SELECT article_id FROM article_projects WHERE project_id = %s
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE article_projects SET project_id = %s WHERE project_id = %s",
            (winner_id, loser_id),
        )
        counts["article_projects"] = cur.rowcount

        # project_sources — UNIQUE (project_id, source_type, source_ref).
        cur.execute(
            """
            DELETE FROM project_sources
            WHERE project_id = %s
              AND (source_type, source_ref) IN (
                  SELECT source_type, source_ref FROM project_sources
                  WHERE project_id = %s
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE project_sources SET project_id = %s WHERE project_id = %s",
            (winner_id, loser_id),
        )
        counts["project_sources"] = cur.rowcount

        # roles — UNIQUE (project_id, firm_id, role, team).
        cur.execute(
            """
            DELETE FROM roles
            WHERE project_id = %s
              AND (firm_id, role, team) IN (
                  SELECT firm_id, role, team FROM roles WHERE project_id = %s
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE roles SET project_id = %s WHERE project_id = %s",
            (winner_id, loser_id),
        )
        counts["roles"] = cur.rowcount

        # claims — UNIQUE (project_id, article_id, md5(text)).
        cur.execute(
            """
            DELETE FROM claims
            WHERE project_id = %s
              AND (article_id, md5(text)) IN (
                  SELECT article_id, md5(text) FROM claims WHERE project_id = %s
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE claims SET project_id = %s WHERE project_id = %s",
            (winner_id, loser_id),
        )
        counts["claims"] = cur.rowcount

        # quotes — UNIQUE (project_id, article_id, md5(text), speaker_name).
        # speaker_name can be NULL so we can't use IN (...) as cleanly.
        cur.execute(
            """
            DELETE FROM quotes loser
            WHERE loser.project_id = %s
              AND EXISTS (
                  SELECT 1 FROM quotes winner
                  WHERE winner.project_id = %s
                    AND winner.article_id = loser.article_id
                    AND md5(winner.text)  = md5(loser.text)
                    AND winner.speaker_name IS NOT DISTINCT FROM loser.speaker_name
              )
            """,
            (loser_id, winner_id),
        )
        cur.execute(
            "UPDATE quotes SET project_id = %s WHERE project_id = %s",
            (winner_id, loser_id),
        )
        counts["quotes"] = cur.rowcount

        # Legacy articles.primary_project_id.
        cur.execute(
            "UPDATE articles SET primary_project_id = %s WHERE primary_project_id = %s",
            (winner_id, loser_id),
        )
        counts["articles_primary_project_id"] = cur.rowcount

        # If the winner has no source_article_id but the loser does, inherit it.
        cur.execute(
            """
            UPDATE projects w
            SET source_article_id = l.source_article_id
            FROM projects l
            WHERE w.id = %s AND l.id = %s
              AND w.source_article_id IS NULL
              AND l.source_article_id IS NOT NULL
            """,
            (winner_id, loser_id),
        )

        cur.execute("DELETE FROM projects WHERE id = %s", (loser_id,))

    conn.commit()
    return counts


# ── Consolidation pass over existing rows ────────────────────────────────────

def consolidate(conn, *, apply: bool, model: str) -> dict:
    """Find duplicate clusters in the existing projects table.

    Each pair `(a, b)` with `a.id < b.id` and `similarity(name) >= NAME_SIM_HARD`
    is reported. With `apply=True`, the lower id wins and the higher id is
    merged in. A union-find over the `merged_into` map keeps later pairs
    consistent when one side has already been absorbed.
    """
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT a.id AS a_id, a.name AS a_name, a.city AS a_city,
                   a.state AS a_state, a.year_completed AS a_year,
                   b.id AS b_id, b.name AS b_name, b.city AS b_city,
                   b.state AS b_state, b.year_completed AS b_year,
                   similarity(a.name, b.name) AS sim
            FROM projects a
            JOIN projects b
              ON a.id < b.id
             AND a.name %% b.name
            WHERE similarity(a.name, b.name) >= %s
            ORDER BY sim DESC
        """, (NAME_SIM_HARD,))
        pairs = cur.fetchall()

    stats = {"clusters_seen": len(pairs), "merges": 0, "rows_repointed": 0}
    merged_into: dict[int, int] = {}

    def _resolve(pid: int) -> int:
        while pid in merged_into:
            pid = merged_into[pid]
        return pid

    for pair in pairs:
        print(json.dumps({k: pair[k] for k in pair}, default=str))
        if not apply:
            continue
        a_resolved = _resolve(pair["a_id"])
        b_resolved = _resolve(pair["b_id"])
        if a_resolved == b_resolved:
            continue
        winner, loser = sorted((a_resolved, b_resolved))
        counts = merge_projects(conn, winner, loser)
        merged_into[loser] = winner
        stats["merges"] += 1
        stats["rows_repointed"] += sum(counts.values())

    return stats


def main():
    p = argparse.ArgumentParser(description="Project entity resolution.")
    p.add_argument("--consolidate", action="store_true",
                   help="Scan existing projects for duplicate clusters.")
    p.add_argument("--apply", action="store_true",
                   help="With --consolidate, apply proposed merges.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()

    conn = get_conn()
    try:
        if args.consolidate:
            stats = consolidate(conn, apply=args.apply, model=args.model)
            print(json.dumps(stats, indent=2), file=sys.stderr)
        else:
            print("Nothing to do. Pass --consolidate, or import resolve_project() from a scraper.",
                  file=sys.stderr)
            sys.exit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
