"""
quotes_v1 — verbatim quotations with speaker attribution.

Version history:
  v1 — initial split. Audit showed two parse failures on long-feature
       articles caused by output-token truncation, not escape errors.
  v2 — explicit JSON escape rules (defense-in-depth) + concision
       guidance (cap surfaces hard limits before truncation strikes).
  v3 — article-title disambiguation for multi-article-page issues
       (Best-of-Year round-ups). Runner now injects an "Article
       title" header; prompt instructs the model to extract quotes
       belonging to the named article only.
"""

from core.probes.registry import ProbeSpec

PROMPT = """You are extracting verbatim quotations from a construction
magazine article.

The runner will prepend an "Article title: ..." header naming the
specific article you are probing. Best-of-Year award round-ups
frequently pack quotes for multiple projects onto the same pages.
Extract ONLY quotes belonging to the named article's subject.
Ignore quotes attached to adjacent award sections or unrelated
projects on the same pages.

Each page is provided as extracted PDF text followed by the page image. The
extracted text is verbatim from the PDF — treat it as the authoritative source
for the quote text and speaker name. The image is for visual context only
(identifying pull quotes vs. inline attribution).

Return a JSON object with one field:

{
  "quotes": [
    {
      "text": "<the verbatim quotation, no surrounding quotes characters>",
      "speaker_name":  <string or null>,
      "speaker_title": <string or null>,
      "speaker_firm":  <string or null>,
      "page": <int>
    }
  ]
}

Rules:
- Verbatim pull quotes or clearly attributed direct speech only.
- Do NOT paraphrase. Quote text must match the printed text exactly.
- If attribution is missing, set speaker fields to null but still
  include the quote.

JSON escape rules (critical — malformed JSON breaks the pipeline):
- Inside the "text" string, escape every double-quote character as \\".
- Smart quotes (" " ' ') are normal characters and need NO escaping.
- Newlines inside text MUST be escaped as \\n. Do not emit raw newlines
  inside a string value.
- If a quote contains an em-dash, ellipsis, or any unicode character,
  emit it literally — do not try to escape it.

Concision:
- Cap output at 12 quotes per article. If there are more, prioritize
  attributed quotes over anonymous ones, and longer / more substantive
  quotes over short interjections.

Return ONLY valid JSON.
"""

SCHEMA = {
    "type": "object",
    "required": ["quotes"],
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "speaker_name": {"type": ["string", "null"]},
                    "speaker_title": {"type": ["string", "null"]},
                    "speaker_firm": {"type": ["string", "null"]},
                    "page": {"type": ["integer", "null"]},
                },
            },
        },
    },
}

PROBE_SPEC = ProbeSpec(
    name="quotes_v1",
    version=3,
    prompt=PROMPT,
    schema_json=SCHEMA,
)
