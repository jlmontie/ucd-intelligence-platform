"""
Cheap deterministic normalizers used by both resolvers as the first
pass before any LLM call. These trim the candidate set so the LLM
only sees genuinely ambiguous cases.
"""

import re

_FIRM_SUFFIXES = (
    "llc", "l.l.c.", "inc", "inc.", "incorporated",
    "corp", "corp.", "corporation", "co", "co.", "company",
    "ltd", "ltd.", "limited",
    "pllc", "pc", "p.c.", "pa", "p.a.",
    "lp", "l.p.", "llp", "l.l.p.",
    "architects", "architecture", "architectural",
    "engineering", "engineers", "consulting", "consultants",
    "construction", "constructors", "contracting",
    "associates", "partners", "group", "studio",
)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s&]")


def normalize_firm_name(name: str) -> str:
    """Lowercase, strip punctuation and trailing legal suffixes."""
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    tokens = s.split(" ")
    while tokens and tokens[-1].rstrip(".") in _FIRM_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_project_name(name: str) -> str:
    """Project-name normalization. Less aggressive than firm names —
    we keep most words and only canonicalize whitespace + punctuation."""
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


_HONORIFICS = ("mr", "mrs", "ms", "miss", "dr", "prof", "professor")
_NAME_SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "p.e.", "pe", "aia")


def normalize_person_name(name: str) -> str:
    """Person-name normalization: lowercase, strip punctuation, drop
    leading honorifics (Mr/Ms/Dr/Prof) and trailing generational /
    credential suffixes (Jr/Sr/III/PE/AIA). Initials lose their
    punctuation so `J. Smith` and `J Smith` collapse together."""
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    tokens = s.split(" ")
    while tokens and tokens[0].rstrip(".") in _HONORIFICS:
        tokens.pop(0)
    while tokens and tokens[-1].rstrip(".") in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)
