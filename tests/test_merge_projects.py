"""
Integration test for core.resolution.merge_projects.

Hits the live dev DB. Creates a synthetic two-project + assorted-children
fixture, runs the merge, asserts every child row was re-pointed and the
loser was deleted, then cleans up. If the test is interrupted partway
through, the leftover rows are obvious (`TEST_MERGE_*`) and easy to delete.

Skipped if get_conn() can't reach the DB — keeps the suite green in CI,
which doesn't have Postgres.
"""

import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.db import dict_cur, get_conn  # noqa: E402, I001
from core.resolution.resolve_projects import merge_projects  # noqa: E402, I001


# Suffix tag used to identify rows belonging to this test, so cleanup is
# unambiguous even on a failed run.
TAG = "TEST_MERGE_PROJECTS"


@pytest.fixture
def conn():
    try:
        c = get_conn()
    except (psycopg2.OperationalError, KeyError, OSError) as e:
        pytest.skip(f"DB unreachable: {e}")
    yield c
    c.close()


def _cleanup(conn) -> None:
    """Delete any rows left tagged with TAG. Cascades handle children."""
    with dict_cur(conn) as cur:
        cur.execute("DELETE FROM articles WHERE title LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM projects WHERE name LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM firms   WHERE name LIKE %s", (f"%{TAG}%",))
        cur.execute("DELETE FROM issues  WHERE filename LIKE %s", (f"%{TAG}%",))
    conn.commit()


def _seed(conn) -> dict:
    """Create a winner project, a loser project, and a representative
    sample of every child relationship so the merge actually has work
    to do. Returns the ids."""
    with dict_cur(conn) as cur:
        cur.execute(
            "INSERT INTO issues (filename) VALUES (%s) RETURNING id",
            (f"{TAG}_issue.pdf",),
        )
        issue_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO articles (issue_id, page_start, page_end, title)
            VALUES (%s, 1, 1, %s) RETURNING id
            """,
            (issue_id, f"{TAG}_article_shared"),
        )
        article_shared = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO articles (issue_id, page_start, page_end, title)
            VALUES (%s, 2, 2, %s) RETURNING id
            """,
            (issue_id, f"{TAG}_article_loser_only"),
        )
        article_loser_only = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO firms (name) VALUES (%s) RETURNING id",
            (f"{TAG}_firm",),
        )
        firm_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO projects (name, source) VALUES (%s, 'corpus') RETURNING id",
            (f"{TAG}_winner",),
        )
        winner = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO projects (name, source, source_article_id)
            VALUES (%s, 'corpus', %s) RETURNING id
            """,
            (f"{TAG}_loser", article_loser_only),
        )
        loser = cur.fetchone()["id"]

        # article_projects on both: shared article on both (collision case),
        # loser-only article on loser (re-point case).
        cur.execute(
            "INSERT INTO article_projects (article_id, project_id, is_primary) "
            "VALUES (%s, %s, TRUE)",
            (article_shared, winner),
        )
        cur.execute(
            "INSERT INTO article_projects (article_id, project_id, is_primary) "
            "VALUES (%s, %s, FALSE)",
            (article_shared, loser),
        )
        cur.execute(
            "INSERT INTO article_projects (article_id, project_id, is_primary) "
            "VALUES (%s, %s, TRUE)",
            (article_loser_only, loser),
        )

        # project_sources: collision on the shared article ref, distinct
        # ref on the loser-only article.
        cur.execute(
            "INSERT INTO project_sources (project_id, source_type, source_ref) "
            "VALUES (%s, 'article', %s)",
            (winner, str(article_shared)),
        )
        cur.execute(
            "INSERT INTO project_sources (project_id, source_type, source_ref) "
            "VALUES (%s, 'article', %s)",
            (loser, str(article_shared)),
        )
        cur.execute(
            "INSERT INTO project_sources (project_id, source_type, source_ref) "
            "VALUES (%s, 'article', %s)",
            (loser, str(article_loser_only)),
        )

        # roles — winner already has the same (firm, role, team), so
        # this loser row should be dropped during merge (collision).
        # A second loser role with a different role string should
        # re-point cleanly.
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'architect', 'design')",
            (winner, firm_id),
        )
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'architect', 'design')",
            (loser, firm_id),
        )
        cur.execute(
            "INSERT INTO roles (project_id, firm_id, role, team) "
            "VALUES (%s, %s, 'engineer', 'design')",
            (loser, firm_id),
        )

        # claims — same idea: one collision, one clean re-point.
        cur.execute(
            "INSERT INTO claims (article_id, project_id, text) VALUES (%s, %s, %s)",
            (article_shared, winner, f"{TAG}_collision_claim"),
        )
        cur.execute(
            "INSERT INTO claims (article_id, project_id, text) VALUES (%s, %s, %s)",
            (article_shared, loser, f"{TAG}_collision_claim"),
        )
        cur.execute(
            "INSERT INTO claims (article_id, project_id, text) VALUES (%s, %s, %s)",
            (article_loser_only, loser, f"{TAG}_unique_claim"),
        )

        # quotes — exercise the speaker_name NULL path too: one quote
        # has a speaker, one doesn't.
        cur.execute(
            "INSERT INTO quotes (article_id, project_id, text, speaker_name) "
            "VALUES (%s, %s, %s, %s)",
            (article_shared, winner, f"{TAG}_collision_quote", "Jane"),
        )
        cur.execute(
            "INSERT INTO quotes (article_id, project_id, text, speaker_name) "
            "VALUES (%s, %s, %s, %s)",
            (article_shared, loser, f"{TAG}_collision_quote", "Jane"),
        )
        cur.execute(
            "INSERT INTO quotes (article_id, project_id, text, speaker_name) "
            "VALUES (%s, %s, %s, NULL)",
            (article_loser_only, loser, f"{TAG}_unique_quote"),
        )
    conn.commit()
    return {
        "winner": winner,
        "loser": loser,
        "article_shared": article_shared,
        "article_loser_only": article_loser_only,
        "firm": firm_id,
    }


def test_merge_repoints_every_child_and_deletes_loser(conn):
    try:
        ids = _seed(conn)
        counts = merge_projects(conn, ids["winner"], ids["loser"])

        # Re-point counts reflect ONLY the rows that survived the dedup
        # pre-pass (the colliding ones were deleted, not re-pointed).
        assert counts["article_projects"] == 1     # loser-only article re-pointed
        assert counts["project_sources"] == 1      # loser-only source re-pointed
        assert counts["roles"] == 1                # 'engineer' role; 'architect' collided
        assert counts["claims"] == 1               # unique_claim; collision_claim dropped
        assert counts["quotes"] == 1               # unique_quote; collision_quote dropped

        with dict_cur(conn) as cur:
            cur.execute(
                "SELECT project_id FROM article_projects WHERE article_id IN (%s,%s) ORDER BY article_id",
                (ids["article_shared"], ids["article_loser_only"]),
            )
            rows = [r["project_id"] for r in cur.fetchall()]
            # Shared article: only the winner row survives (loser dup dropped).
            # Loser-only article: re-pointed to winner.
            assert rows == [ids["winner"], ids["winner"]]

            cur.execute(
                "SELECT source_ref FROM project_sources WHERE project_id = %s ORDER BY source_ref",
                (ids["winner"],),
            )
            refs = [r["source_ref"] for r in cur.fetchall()]
            assert refs == sorted([str(ids["article_shared"]), str(ids["article_loser_only"])])

            cur.execute(
                "SELECT role FROM roles WHERE project_id = %s ORDER BY role",
                (ids["winner"],),
            )
            assert [r["role"] for r in cur.fetchall()] == ["architect", "engineer"]

            cur.execute(
                "SELECT text FROM claims WHERE project_id = %s ORDER BY text",
                (ids["winner"],),
            )
            assert [r["text"] for r in cur.fetchall()] == [
                f"{TAG}_collision_claim",
                f"{TAG}_unique_claim",
            ]

            cur.execute(
                "SELECT text, speaker_name FROM quotes WHERE project_id = %s "
                "ORDER BY text",
                (ids["winner"],),
            )
            quote_rows = cur.fetchall()
            assert [(q["text"], q["speaker_name"]) for q in quote_rows] == [
                (f"{TAG}_collision_quote", "Jane"),
                (f"{TAG}_unique_quote", None),
            ]

            cur.execute("SELECT 1 FROM projects WHERE id = %s", (ids["loser"],))
            assert cur.fetchone() is None

            # source_article_id was inherited (winner had NULL, loser had article_loser_only).
            cur.execute(
                "SELECT source_article_id FROM projects WHERE id = %s", (ids["winner"],)
            )
            assert cur.fetchone()["source_article_id"] == ids["article_loser_only"]
    finally:
        _cleanup(conn)


def test_merge_rejects_self_merge(conn):
    try:
        ids = _seed(conn)
        with pytest.raises(ValueError, match="distinct"):
            merge_projects(conn, ids["winner"], ids["winner"])
    finally:
        _cleanup(conn)


def test_merge_rejects_unknown_id(conn):
    try:
        ids = _seed(conn)
        with pytest.raises(ValueError, match="not found"):
            merge_projects(conn, ids["winner"], 10**9)
    finally:
        _cleanup(conn)
