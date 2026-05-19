"""
project_panel_v1 — structured project facts + team rosters.

One of three probes carved out of the original monolithic extraction
prompt (ingest_corpus/ingest.py:183). Focuses on the project info
panel, roster credits, and article-level metadata (byline). Claims
and quotes are siblings.

Version history:
  v1 — initial split. Audit found year_completed extracted as null
       on every project (incl. ones explicitly labeled completed) and
       no author / byline extraction at all.
  v2 — explicit instruction to mine year_completed from body copy
       when the panel is silent; adds top-level `author` for the
       article byline.
  v3 — explicit JSON escape rules + concision cap + tightened author
       rule (return only the name, no "By " prefix). Aligns with the
       quotes_v1 / claims_v1 v2 hardening.
  v4 — explicit article-title disambiguation. Best-of-Year award
       round-ups pack 3+ project features onto the same pages; v3
       silently picked the most-content-rich project from the page
       text, regardless of which article the runner was supposed to
       be probing. The runner now prepends an "Article title: ..."
       header to each call, and the prompt instructs the model to
       extract data ONLY for the named article — ignoring adjacent
       content about other awards on the same pages.
"""

from core.probes.registry import ProbeSpec

PROMPT = """You are extracting the project info panel, team rosters, and
article-level metadata from a construction magazine article.

The runner will prepend an "Article title: ..." header naming the
specific article you are probing. Best-of-Year award round-ups
frequently pack multiple project features onto the same pages — the
page text may contain content about projects OTHER than the named
one. Extract ONLY data for the named article's project. Ignore
adjacent award sections ("Most Outstanding X Project: ..."),
sidebars, and unrelated projects mentioned on the same pages.

Each page is provided as extracted PDF text followed by the page image. The
extracted text is verbatim from the PDF — treat it as the authoritative source
for all specific facts: numbers, dollar amounts, square footages, firm names,
bylines. Use the image only for visual layout context (identifying info
panels, pull quotes, bylines).

Return a single JSON object:

{
  "summary": "<2-3 sentence summary of the article>",
  "author": <article byline / "By X" line, string or null>,
  "project": {
    "name": <string or null>,
    "typology": <"K-12"|"higher_ed"|"healthcare"|"industrial"|"multifamily"|"mixed_use"|"office"|"aviation"|"hospitality"|"civic"|"religious"|"recreation"|"infrastructure"|"senior_living"|"retail"|"other"|null>,
    "location": <string or null>,
    "city": <string or null>,
    "state": <2-letter code or null>,
    "cost": <original string e.g. "$45,900,000" or null>,
    "square_footage": <original string e.g. "34,000 SF" or null>,
    "stories_levels": <string or null>,
    "delivery_method": <string or null>,
    "year_completed": <4-digit int or null>,
    "status": <"completed"|"under_construction"|"announced"|null>
  },
  "design_team": [{"role": <string>, "firm": <string>}],
  "construction_team": [{"role": <string>, "firm": <string>}],
  "owner": <string or null>,
  "owner_rep": <string or null>,
  "developer": <string or null>
}

Rules for `author`:
- Look for "By <Name>" / "Author: <Name>" / a byline near the title.
- Return ONLY the name. Do NOT include the "By " or "Author:" prefix.
- If the byline is "Staff" or absent, return null.

JSON escape rules (critical — malformed JSON breaks the pipeline):
- Inside any string value, escape every double-quote as \\".
- Smart quotes (" " ' ') are normal characters and need NO escaping.
- Newlines inside text MUST be escaped as \\n. Do not emit raw newlines
  inside a string value.
- Emit unicode (em-dash, ellipsis, etc.) literally; do not try to escape it.

Rules for `year_completed`:
- The info panel often omits the year. Mine the body copy: phrases
  like "opened in 2024", "completed in fall 2023", "ribbon-cutting
  in May 2025" all yield a year. If the article describes the
  project as completed but states no year explicitly, return null.
- Do NOT use the article's publication date as a substitute. Only
  return a year that's stated as the project's completion year.

Rules for teams:
- Extract every firm listed in the project info panel. Use the role exactly as printed.
- If a role has multiple firms, emit one entry per firm.
- Do NOT invent roles or firms not explicitly printed.

If this is not a project feature, return project as null and empty teams.
Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "required": ["summary", "project", "design_team", "construction_team"],
    "properties": {
        "summary": {"type": ["string", "null"]},
        "author":  {"type": ["string", "null"]},
        "project": {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": ["string", "null"]},
                "typology": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "city": {"type": ["string", "null"]},
                "state": {"type": ["string", "null"]},
                "cost": {"type": ["string", "null"]},
                "square_footage": {"type": ["string", "null"]},
                "stories_levels": {"type": ["string", "null"]},
                "delivery_method": {"type": ["string", "null"]},
                "year_completed": {"type": ["integer", "null"]},
                "status": {"type": ["string", "null"]},
            },
        },
        "design_team": {"type": "array",
                        "items": {"type": "object",
                                  "required": ["role", "firm"]}},
        "construction_team": {"type": "array",
                              "items": {"type": "object",
                                        "required": ["role", "firm"]}},
        "owner": {"type": ["string", "null"]},
        "owner_rep": {"type": ["string", "null"]},
        "developer": {"type": ["string", "null"]},
    },
}

PROBE_SPEC = ProbeSpec(
    name="project_panel_v1",
    version=4,
    prompt=PROMPT,
    schema_json=SCHEMA,
)
