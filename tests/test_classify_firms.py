"""
Unit tests for the rule-based firm-type classifier.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.resolution.classify_firms import classify_role  # noqa: E402, I001


@pytest.mark.parametrize("role,team,expected", [
    # Owner team
    ("Owner",                       "owner",        "owner"),
    ("Owner / Developer",           "owner",        "developer"),
    ("Developer",                   "owner",        "developer"),

    # Construction team
    ("General Contractor",          "construction", "contractor"),
    ("Construction Manager",        "construction", "contractor"),
    ("CM/GC",                       "construction", "contractor"),
    ("Subcontractor: Steel",        "construction", "subcontractor"),
    ("Cost Consultant",             "construction", "consultant"),

    # Design team
    ("Architect",                   "design",       "architect"),
    ("Architect of Record",         "design",       "architect"),
    ("Design Architect",            "design",       "architect"),
    ("Structural Engineer",         "design",       "engineer"),
    ("MEP Engineer",                "design",       "engineer"),
    ("Civil Engineer",              "design",       "engineer"),
    ("Engineer",                    "design",       "engineer"),
    ("Landscape Designer",          "design",       "consultant"),
    ("Interior Designer",           "design",       "consultant"),
    ("Acoustical Consultant",       "design",       "consultant"),

    # Unknown teams
    ("",                            "",             "other"),
    ("Architect",                   "",             "other"),
])
def test_classify_role(role, team, expected):
    assert classify_role(role, team) == expected
