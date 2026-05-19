"""
claims_v1 — notable factual assertions extracted from an article.

Version history:
  v1 — initial split.
  v2 — explicit JSON escape rules + concision cap; aligns with the
       quotes_v1 v2 hardening.
  v3 — article-title disambiguation for multi-article-page issues
       (Best-of-Year round-ups). Runner now injects an "Article
       title" header; prompt instructs the model to attribute claims
       ONLY to the named article.
"""

from core.probes.registry import ProbeSpec

PROMPT = """You are extracting notable factual claims from a construction
magazine article.

The runner will prepend an "Article title: ..." header naming the
specific article you are probing. The page text may include content
from other articles on the same pages (common in Best-of-Year award
round-ups). Extract ONLY claims about the named article's subject.
Ignore claims attached to adjacent award sections, sidebars, or
unrelated projects on the same pages.

Each page is provided as extracted PDF text followed by the page image. The
extracted text is verbatim from the PDF — treat it as the authoritative source
for all specific facts. The image is for visual context only.

Return a JSON object with one field:

{
  "claims": [
    {
      "text": "<the claim, paraphrased to be self-contained>",
      "type": "stat" | "milestone" | "challenge" | "award" | "first" | "other",
      "page": <int>
    }
  ]
}

Rules:
- Notable factual assertions only: statistics, superlatives, milestones,
  challenges, awards, regional firsts.
- Include the page number the claim appears on.
- Ignore marketing boilerplate, generic descriptions, and quotations
  (those go to a separate probe).
- Make each claim self-contained: a reader should be able to understand
  it without the surrounding paragraph.

JSON escape rules (critical — malformed JSON breaks the pipeline):
- Inside the "text" string, escape every double-quote character as \\".
- Smart quotes (" " ' ') are normal characters and need NO escaping.
- Newlines inside text MUST be escaped as \\n. Do not emit raw newlines
  inside a string value.
- Emit unicode (em-dash, ellipsis, etc.) literally; do not try to escape it.

Concision:
- Cap output at 15 claims per article. If there are more, prioritize
  the most distinctive / quantitative claims.

Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "type"],
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string"},
                    "page": {"type": ["integer", "null"]},
                },
            },
        },
    },
}

PROBE_SPEC = ProbeSpec(
    name="claims_v1",
    version=3,
    prompt=PROMPT,
    schema_json=SCHEMA,
)
