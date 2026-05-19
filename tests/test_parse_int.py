"""
Static unit tests for ingest_corpus.ingest._parse_int.

Captures the magnitude-parsing logic the audit caught silently
corrupting `$100 million` -> 100. Runs in CI without a DB.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from ingest_corpus.ingest import _clean_byline, _extract_scope, _parse_int  # noqa: E402, I001


@pytest.mark.parametrize("text,expected", [
    ("$45,900,000",   45_900_000),
    ("$100 million",  100_000_000),
    ("$5 billion",    5_000_000_000),
    ("$1.5 million",  1_500_000),
    ("$2.3M",         2_300_000),
    ("$5B",           5_000_000_000),
    ("1,200",         1_200),
    ("34,000",        34_000),
    ("86,000 sf",     86_000),
    (None,            None),
    ("",              None),
    ("TBD",           None),
    # Multi-number strings: take the FIRST number, do not concatenate.
    # Caught a real corpus-run crash where probe output for sq_ft was
    # 'Total building footprint: 300,000 SF, including 130,000 SF'
    # and the legacy parser produced 300_000_130_000, overflowing
    # sq_ft INTEGER and killing the ingest mid-stream.
    ("Total building footprint: 300,000 SF, including 130,000 SF", 300_000),
    ("300,000-450,000 SF",                                          300_000),
    ("approximately 50,000 sf across 3 buildings",                  50_000),
])
def test_parse_int(text, expected):
    assert _parse_int(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("By Jane Smith",       "Jane Smith"),
    ("by jane smith",       "jane smith"),
    ("BY  Jane  Smith",     "Jane  Smith"),
    ("Author: Jane Smith",  "Jane Smith"),
    ("author:Jane Smith",   "Jane Smith"),
    ("Jane Smith",          "Jane Smith"),
    ("Staff",               None),
    ("staff",               None),
    ("",                    None),
    (None,                  None),
    ("By ",                 None),
])
def test_clean_byline(text, expected):
    assert _clean_byline(text) == expected


@pytest.mark.parametrize("text,clean,scope", [
    ("Flynn Companies",                        "Flynn Companies",        None),
    ("Flynn Companies (patching)",             "Flynn Companies",        "patching"),
    ("Roofing (patching)",                     "Roofing",                "patching"),
    ("Glazing/Curtain Wall (interior)",        "Glazing/Curtain Wall",   "interior"),
    ("FW Specialties (terrazzo)",              "FW Specialties",         "terrazzo"),
    # Multiple parentheticals
    ("Foo (bar) Bar (baz)",                    "Foo Bar",                "bar; baz"),
    # Empty parentheses are ignored
    ("Plain Firm ()",                          "Plain Firm",             None),
    # Edge cases
    (None,                                     None,                     None),
    ("",                                       "",                       None),
])
def test_extract_scope(text, clean, scope):
    assert _extract_scope(text) == (clean, scope)
