#!/usr/bin/env python3
"""
Upsert every ProbeSpec in core.probes.REGISTRY into the `probes` table.

Required before run_probes() can write probe_runs (probe_runs.probe_id
is an FK).

Behavior:
  - First seen (name, version) → INSERT.
  - Same (name, version) → no-op (definitions are immutable per version).
  - New version of an existing name → INSERT a new row, leave the old
    row in place so historical probe_runs keep their FK target.

Usage:
    python -m core.probes.seed
"""

import json
import sys

from core.db import dict_cur, get_conn
from core.probes import REGISTRY


def seed(conn) -> dict:
    stats = {"inserted": 0, "unchanged": 0}
    with dict_cur(conn) as cur:
        for spec in REGISTRY.values():
            cur.execute(
                "SELECT id FROM probes WHERE name=%s AND version=%s",
                (spec.name, spec.version),
            )
            if cur.fetchone():
                stats["unchanged"] += 1
                continue
            cur.execute(
                """
                INSERT INTO probes (name, version, prompt, schema_json, model, active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                """,
                (
                    spec.name,
                    spec.version,
                    spec.prompt,
                    json.dumps(spec.schema_json),
                    spec.model,
                ),
            )
            stats["inserted"] += 1
    conn.commit()
    return stats


def main():
    conn = get_conn()
    try:
        stats = seed(conn)
    finally:
        conn.close()
    print(json.dumps(stats, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
