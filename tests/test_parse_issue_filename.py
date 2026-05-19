"""
Static unit tests for ingest_corpus.ingest.parse_issue_filename.

No DB needed; runs in CI alongside the schema contract test.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from ingest_corpus.ingest import parse_issue_filename  # noqa: E402, I001


@pytest.mark.parametrize("filename,expected", [
    ("UC-D+February+2026-spreads.pdf",   (2026, "February")),
    ("2020_APRIL20.spreads-2.pdf",       (2020, "April")),
    ("2020_AUG_.Spreads.pdf",            (2020, "August")),
    ("2020_DecSpreads.pdf",              (2020, "December")),  # CamelCase boundary
    ("2020_Feb_.Spreads.pdf",            (2020, "February")),
    ("aug | sept_spreads_2016.pdf",      (2016, "August")),    # double-issue picks first
    ("UC-D+October+2018.pdf",            (2018, "October")),
    ("UC-D+May+2019-spreads.pdf",        (2019, "May")),       # short month name
    ("Marquardt-2024-spreads.pdf",       (2024, None)),        # 'Mar' inside word: no match
    ("Decision-2025-special.pdf",        (2025, None)),        # 'Dec' followed by lowercase: no match
    ("something_else.pdf",               (None, None)),
])
def test_parse_issue_filename(filename, expected):
    assert parse_issue_filename(filename) == expected
