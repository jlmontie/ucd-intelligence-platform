"""
Integration test for core.resolution.consolidate.merge_firms.

Mirrors tests/test_merge_projects.py. Creates a synthetic two-firm
fixture plus a representative sample of every child relationship,
runs the merge, asserts the survivors line up. Cleans up after itself
regardless of pass/fail (TAG-prefixed rows are obvious leftovers if a
run is interrupted).
"""

import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.db import dict_cur, get_conn  # noqa: E402, I001
from core.resolution.consolidate import merge_firms  # noqa: E402, I001

TAG = "TEST_MERGE_FIRMS"


def _cleanup(conn) -> None:
    """Remove every row tagged with TAG. Rollback first so an aborted
    transaction from a prior failure doesn't block the deletes."""
    conn.rollback()
    with dict_cur(conn) as cur:
        # Order matters: drop FK-referencing rows before referenced ones.
        cur.execute("DELETE FROM firm_mentions WHERE raw_text LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM articles  WHERE title    LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM projects  WHERE name     LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM people    WHERE name     LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM firms     WHERE name     LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM issues    WHERE filename LIKE %s", (f"%{TAG}%",))
    conn.commit()


@pytest.fixture
def conn():
    try:
        c = get_conn()
    except (psycopg2.OperationalError, KeyError, OSError) as e:
        pytest.skip(f"DB unreachable: {e}")
    _cleanup(c)   # purge any leftover state from a failed prior run
    yield c
    _cleanup(c)
    c.close()


def _seed(conn) -> dict:
    """Two firms, one project, three roles (one collision case),
    two people (one collision case), some firm_mentions."""
    with dict_cur(conn) as cur:
        cur.execute(
            "INSERT INTO issues (filename) VALUES (%s) RETURNING id",
            (f"{TAG}_issue.pdf",),
        )
        issue_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO articles (issue_id, page_start, page_end, title) "
            "VALUES (%s, 1, 1, %s)",
            (issue_id, f"{TAG}_article"),
        )

        cur.execute(
            "INSERT INTO firms (name, aliases) VALUES (%s, %s::jsonb) RETURNING id",
            (f"{TAG}_winner", '["existing_alias"]'),
        )
        winner = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO firms (name, aliases) VALUES (%s, %s::jsonb) RETURNING id",
            (f"{TAG}_loser (qualifier)", '["loser_alias"]'),
        )
        loser = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO projects (name, source) VALUES (%s, 'corpus') RETURNING id",
            (f"{TAG}_project",),
        )
        project_id = cur.fetchone()["id"]

        # roles — winner has (architect, design); loser has same (collision)
        # plus distinct (engineer, design).
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'architect', 'design')",
            (project_id, winner),
        )
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'architect', 'design')",
            (project_id, loser),
        )
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'engineer', 'design')",
            (project_id, loser),
        )

        # people — winner has 'Alice'; loser also has 'Alice' (collision)
        # plus 'Bob' (clean re-point).
        cur.execute(
            "INSERT INTO people (name, firm_id) VALUES (%s, %s) RETURNING id",
            (f"{TAG}_Alice", winner),
        )
        cur.execute(
            "INSERT INTO people (name, firm_id) VALUES (%s, %s) RETURNING id",
            (f"{TAG}_Alice", loser),
        )
        cur.execute(
            "INSERT INTO people (name, firm_id) VALUES (%s, %s) RETURNING id",
            (f"{TAG}_Bob", loser),
        )

        # firm_mentions — no uniqueness; both rows just re-point.
        cur.execute(
            "INSERT INTO firm_mentions (raw_text, canonical_id) VALUES (%s, %s)",
            (f"{TAG}_mention_w", winner),
        )
        cur.execute(
            "INSERT INTO firm_mentions (raw_text, canonical_id) VALUES (%s, %s)",
            (f"{TAG}_mention_l", loser),
        )

    conn.commit()
    return {"winner": winner, "loser": loser, "project_id": project_id}


def test_merge_repoints_collisions_and_inherits_alias(conn):
    try:
        ids = _seed(conn)
        counts = merge_firms(conn, ids["winner"], ids["loser"])

        # Re-point counts reflect only what wasn't pre-deleted for collision.
        assert counts["roles"] == 1            # 'engineer' re-pointed; 'architect' collided
        assert counts["people"] == 1           # 'Bob' re-pointed; 'Alice' collided
        assert counts["firm_mentions"] == 1    # plain re-point

        with dict_cur(conn) as cur:
            cur.execute(
                "SELECT role FROM roles WHERE project_id = %s AND firm_id = %s ORDER BY role",
                (ids["project_id"], ids["winner"]),
            )
            assert [r["role"] for r in cur.fetchall()] == ["architect", "engineer"]

            cur.execute(
                "SELECT name FROM people WHERE firm_id = %s ORDER BY name",
                (ids["winner"],),
            )
            assert [r["name"] for r in cur.fetchall()] == [
                f"{TAG}_Alice", f"{TAG}_Bob",
            ]

            cur.execute(
                "SELECT canonical_id FROM firm_mentions WHERE raw_text LIKE %s",
                (f"{TAG}_%",),
            )
            assert {r["canonical_id"] for r in cur.fetchall()} == {ids["winner"]}

            cur.execute("SELECT 1 FROM firms WHERE id = %s", (ids["loser"],))
            assert cur.fetchone() is None

            # Winner picked up loser's name + loser's prior aliases.
            cur.execute("SELECT aliases FROM firms WHERE id = %s", (ids["winner"],))
            aliases = cur.fetchone()["aliases"]
            assert f"{TAG}_loser (qualifier)" in aliases
            assert "loser_alias" in aliases
            assert "existing_alias" in aliases
    finally:
        _cleanup(conn)


def test_merge_rejects_self_merge(conn):
    try:
        ids = _seed(conn)
        with pytest.raises(ValueError, match="distinct"):
            merge_firms(conn, ids["winner"], ids["winner"])
    finally:
        _cleanup(conn)


def test_merge_rejects_unknown_id(conn):
    try:
        ids = _seed(conn)
        with pytest.raises(ValueError, match="not found"):
            merge_firms(conn, ids["winner"], 10**9)
    finally:
        _cleanup(conn)
