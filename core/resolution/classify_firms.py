#!/usr/bin/env python3
"""
Firm-type classification.

Reads the roles a firm plays across all projects and assigns
`firms.firm_type` from the constrained enum. When a firm shows up in
multiple distinct categories (e.g., owner on one project, developer
on another), the dominant role becomes `firm_type` and the rest go
into `firm_type_aux`.

Rule-based by design: this is fool-proof for a less intelligent model
to follow, runs in seconds, and stays correct as the corpus grows.
The LLM has already done the hard work — labeling each role string in
the probe — so all that's needed here is consistent bucketing.

Usage:
    python -m core.resolution.classify_firms              # update all firms
    python -m core.resolution.classify_firms --only-unknown  # only those at default
    python -m core.resolution.classify_firms --dry-run    # print plan, don't write
"""

import argparse
import json
import sys
from collections import Counter

from core.db import dict_cur, get_conn

# Allowed values must match the CHECK constraint in db/schema.sql.
ALLOWED = (
    "architect", "engineer", "contractor",
    "owner", "developer", "consultant", "subcontractor",
    "other", "unknown",
)


def classify_role(role: str, team: str) -> str:
    """Map a single (role, team) pair to a firm_type bucket.

    Order matters: more specific labels checked first. Sub-disciplines
    (`Structural Engineer`, `MEP Engineer`) collapse to `engineer`;
    architectural sub-disciplines collapse to `architect`."""
    role_l = (role or "").lower()
    team_l = (team or "").lower()

    # Owner team: separate developer (someone who finances + holds
    # the asset for sale/lease) from owner (end user / institution).
    if team_l == "owner":
        if "develop" in role_l:
            return "developer"
        return "owner"

    # Construction team buckets
    if team_l == "construction":
        if "subcontract" in role_l:
            return "subcontractor"
        if "consultant" in role_l:
            return "consultant"
        # Catch-all: General Contractor, Construction Manager, CM/GC
        return "contractor"

    # Design team buckets
    if team_l == "design":
        if "architect" in role_l:
            return "architect"
        if "engineer" in role_l:
            return "engineer"
        if "consultant" in role_l or "advisor" in role_l:
            return "consultant"
        # Landscape designer, interior designer, etc. roll up to consultant.
        return "consultant"

    return "other"


def collect_firm_classifications(conn) -> dict[int, Counter]:
    """For each firm, return a Counter of bucket -> occurrence count
    across all its `roles` rows. Firms with no roles are absent from
    the result and stay at firm_type='unknown'."""
    out: dict[int, Counter] = {}
    with dict_cur(conn) as cur:
        cur.execute("SELECT firm_id, role, team FROM roles WHERE firm_id IS NOT NULL")
        for row in cur.fetchall():
            bucket = classify_role(row["role"], row["team"])
            if bucket not in ALLOWED:
                bucket = "other"
            out.setdefault(row["firm_id"], Counter())[bucket] += 1
    return out


def plan_updates(
    conn,
    *,
    only_unknown: bool,
) -> list[dict]:
    """Compute the (firm_id, current, new_type, new_aux) tuples that
    would change. Idempotent — already-correct rows produce no diff."""
    classifications = collect_firm_classifications(conn)

    with dict_cur(conn) as cur:
        cur.execute("SELECT id, name, firm_type, firm_type_aux FROM firms")
        firms = cur.fetchall()

    plan: list[dict] = []
    for f in firms:
        if only_unknown and f["firm_type"] != "unknown":
            continue
        counts = classifications.get(f["id"])
        if not counts:
            continue  # no role data; leave at 'unknown'

        ranked = counts.most_common()
        new_type = ranked[0][0]
        # Auxes are everything except the dominant, sorted for stability.
        new_aux = sorted(b for b, _n in ranked[1:])

        if new_type == f["firm_type"] and new_aux == (f["firm_type_aux"] or []):
            continue
        plan.append({
            "firm_id": f["id"],
            "name": f["name"],
            "old_type": f["firm_type"],
            "new_type": new_type,
            "old_aux": f["firm_type_aux"] or [],
            "new_aux": new_aux,
            "evidence": dict(counts),
        })
    return plan


def apply_updates(conn, plan: list[dict]) -> int:
    """Apply each update in a single commit. Returns rows changed."""
    if not plan:
        return 0
    with dict_cur(conn) as cur:
        for p in plan:
            cur.execute(
                "UPDATE firms SET firm_type=%s, firm_type_aux=%s WHERE id=%s",
                (p["new_type"], json.dumps(p["new_aux"]), p["firm_id"]),
            )
    conn.commit()
    return len(plan)


def main():
    p = argparse.ArgumentParser(description="Classify firms by firm_type from their roles.")
    p.add_argument("--only-unknown", action="store_true",
                   help="Only update firms currently at firm_type='unknown'.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan but don't write.")
    args = p.parse_args()

    conn = get_conn()
    try:
        plan = plan_updates(conn, only_unknown=args.only_unknown)
        for entry in plan:
            print(json.dumps(entry, default=str))
        if not args.dry_run:
            n = apply_updates(conn, plan)
            print(f"\napplied {n} update(s)", file=sys.stderr)
        else:
            print(f"\n--dry-run: would update {len(plan)} firm(s)", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
