"""
Schema contract test.

Static parse of db/schema.sql — no database required, so this runs in
CI without spinning up Postgres. Asserts that every column and table
both ingest tracks rely on is present and survives future edits.

The contract is intentionally minimal: it locks down the columns named
in plan §2.1 and the few legacy columns the existing pipeline depends
on. It does NOT validate types, indexes, or constraints — Postgres
catches those at apply time. The point is to fail fast on a PR that
accidentally drops a load-bearing column.
"""

import re
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


# ── Contract definition ──────────────────────────────────────────────────────
# (table, [columns that must be defined on it])

REQUIRED: dict[str, list[str]] = {
    "issues": [
        "id", "filename", "year", "month_label", "page_count", "ingested_at",
    ],
    "articles": [
        "id", "issue_id", "page_start", "page_end",
        "title", "author", "article_type", "summary",
        "primary_project_id", "content_hash", "embedding", "ingested_at",
    ],
    "projects": [
        "id", "name", "typology", "location", "city", "state",
        "county", "lat", "lng",
        "cost", "cost_usd", "estimated_cost_usd",
        "square_footage", "sq_ft", "stories_levels", "delivery_method",
        "year_completed", "status", "phase", "source",
        "source_article_id", "embedding",
    ],
    "article_projects": ["article_id", "project_id", "is_primary"],
    "project_sources": [
        "id", "project_id", "source_type", "source_ref",
        "confidence", "first_seen", "last_seen",
    ],
    "firms": [
        "id", "name", "aliases", "firm_type", "firm_type_aux",
        "website", "notes",
    ],
    "people": [
        "id", "name", "title", "firm_id", "aliases", "confidence", "notes",
    ],
    "roles": [
        "id", "project_id", "firm_id", "role", "team",
        "raw_name", "confidence",
    ],
    "claims": [
        "id", "article_id", "project_id", "text", "type",
        "page", "confidence", "embedding",
    ],
    "quotes": [
        "id", "article_id", "project_id",
        "speaker_name", "speaker_title", "speaker_firm",
        "speaker_person_id", "text", "page",
    ],
    "probes": [
        "id", "name", "version", "prompt", "schema_json", "model", "active",
    ],
    "probe_runs": [
        "id", "probe_id", "article_id", "probe_version",
        "model", "content_hash", "output_json", "ran_at",
    ],
    "firm_mentions": [
        "id", "raw_text", "canonical_id", "confidence", "corrected",
    ],
    "person_mentions": [
        "id", "raw_name", "raw_title", "raw_firm",
        "canonical_id", "confidence", "corrected",
    ],
}


# ── Static parser ────────────────────────────────────────────────────────────

_TABLE_BLOCK = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_ADD_COL = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+(.*?);",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def _parse_columns(body: str) -> list[str]:
    """Extract column identifiers from a CREATE TABLE body. Skips
    table-level constraints (lines starting with PRIMARY KEY, FOREIGN
    KEY, UNIQUE, CONSTRAINT, CHECK)."""
    cols: list[str] = []
    # Split on commas at parenthesis depth 0.
    depth = 0
    current = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            cols.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        cols.append("".join(current))

    out: list[str] = []
    for raw in cols:
        s = raw.strip()
        if not s:
            continue
        first = s.split()[0].upper()
        if first in {"PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"}:
            continue
        m = re.match(r'"?(\w+)"?', s)
        if m:
            out.append(m.group(1))
    return out


_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- ...` line comments. Block comments aren't used in
    schema.sql so we don't bother handling them."""
    return _LINE_COMMENT.sub("", sql)


def _index_schema() -> dict[str, set[str]]:
    """Return {table: {column, ...}} from the current schema.sql.

    Honors both initial CREATE TABLE definitions and subsequent
    ALTER TABLE ... ADD COLUMN statements."""
    text = _strip_sql_comments(SCHEMA_PATH.read_text())
    schema: dict[str, set[str]] = {}

    for m in _TABLE_BLOCK.finditer(text):
        table = m.group(1)
        body = m.group(2)
        schema.setdefault(table, set()).update(_parse_columns(body))

    for m in _ALTER_ADD_COL.finditer(text):
        table = m.group(1)
        for col_match in _ADD_COLUMN.finditer(m.group(2)):
            schema.setdefault(table, set()).add(col_match.group(1))

    return schema


@pytest.fixture(scope="module")
def schema() -> dict[str, set[str]]:
    assert SCHEMA_PATH.exists(), f"schema not found at {SCHEMA_PATH}"
    return _index_schema()


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", list(REQUIRED.keys()))
def test_table_present(table: str, schema):
    assert table in schema, (
        f"required table `{table}` missing from db/schema.sql"
    )


@pytest.mark.parametrize(
    "table,column",
    [(t, c) for t, cols in REQUIRED.items() for c in cols],
)
def test_required_column(table: str, column: str, schema):
    cols = schema.get(table, set())
    assert column in cols, (
        f"`{table}.{column}` missing from db/schema.sql; "
        f"both ingest tracks rely on this column"
    )


def test_projects_source_default_corpus(schema):
    """Sanity: the source check is meaningful only if the column exists
    AND the schema enumerates the three valid values somewhere."""
    text = SCHEMA_PATH.read_text()
    for v in ("'corpus'", "'public_data'", "'merged'"):
        assert v in text, f"projects.source enum value {v} missing"


def test_project_sources_types_enumerated():
    text = SCHEMA_PATH.read_text()
    for v in (
        "'article'", "'up3_solicitation'", "'dfcm_listing'",
        "'stip_entry'", "'appropriation_line'",
        "'recorder_filing'", "'planning_agenda'",
    ):
        assert v in text, f"project_sources.source_type value {v} missing"
