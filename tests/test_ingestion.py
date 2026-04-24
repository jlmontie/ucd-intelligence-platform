"""
Validation tests for Stage 0 ingestion.

Run after ingesting a small set of issues (--limit 3) to verify quality
before committing to the full corpus.

Usage:
    pytest tests/test_ingestion.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.db import dict_cur, get_conn


@pytest.fixture(scope="module")
def conn():
    c = get_conn()
    yield c
    c.close()


# ── Basic completeness ────────────────────────────────────────────────────────

def test_issues_ingested(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM issues")
        n = cur.fetchone()["n"]
    assert n > 0, "No issues in DB — run ingest.py first"


def test_articles_present(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM articles")
        n = cur.fetchone()["n"]
    assert n > 0, "No articles extracted"


def test_projects_present(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM projects")
        n = cur.fetchone()["n"]
    assert n > 0, "No projects extracted"


def test_firms_present(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM firms")
        n = cur.fetchone()["n"]
    assert n > 0, "No firms extracted"


def test_roles_present(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM roles")
        n = cur.fetchone()["n"]
    assert n > 0, "No roles extracted"


# ── Ratio sanity checks ───────────────────────────────────────────────────────

def test_projects_per_issue_ratio(conn):
    """Expect roughly 3–12 projects per issue."""
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS issues FROM issues")
        issues = cur.fetchone()["issues"]
        cur.execute("SELECT COUNT(*) AS projects FROM projects")
        projects = cur.fetchone()["projects"]
    if issues > 0:
        ratio = projects / issues
        assert 2 <= ratio <= 20, f"Unusual projects/issue ratio: {ratio:.1f} ({projects} projects, {issues} issues)"


def test_roles_per_project_ratio(conn):
    """Expect roughly 5–30 firm roles per project."""
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS projects FROM projects")
        projects = cur.fetchone()["projects"]
        cur.execute("SELECT COUNT(*) AS roles FROM roles")
        roles = cur.fetchone()["roles"]
    if projects > 0:
        ratio = roles / projects
        assert 3 <= ratio <= 50, f"Unusual roles/project ratio: {ratio:.1f}"


def test_claims_and_quotes_extracted(conn):
    with dict_cur(conn) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM claims")
        claims = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM quotes")
        quotes = cur.fetchone()["n"]
    assert claims > 0, "No claims extracted — check extraction prompt"
    assert quotes > 0, "No quotes extracted — check extraction prompt"


# ── Spot-check known data (Feb 2026 issue) ───────────────────────────────────

def test_known_project_present(conn):
    """Delta Sky Club should be in the Feb 2026 issue."""
    with dict_cur(conn) as cur:
        cur.execute(
            "SELECT id FROM projects WHERE name ILIKE %s",
            ("%Delta Sky Club%",),
        )
        row = cur.fetchone()
    assert row is not None, "Known project 'Delta Sky Club' not found — check Feb 2026 extraction"


def test_known_firm_present(conn):
    """HOK should appear as a firm."""
    with dict_cur(conn) as cur:
        cur.execute("SELECT id FROM firms WHERE name ILIKE %s", ("%HOK%",))
        row = cur.fetchone()
    assert row is not None, "Known firm 'HOK' not found"


def test_known_role_present(conn):
    """HOK should be listed as Architect on the Delta Sky Club project."""
    with dict_cur(conn) as cur:
        cur.execute("""
            SELECT r.id FROM roles r
            JOIN firms f ON f.id = r.firm_id
            JOIN projects p ON p.id = r.project_id
            WHERE f.name ILIKE %s AND p.name ILIKE %s AND r.role ILIKE %s
        """, ("%HOK%", "%Delta Sky Club%", "%Architect%"))
        row = cur.fetchone()
    assert row is not None, "HOK not found as Architect on Delta Sky Club"
