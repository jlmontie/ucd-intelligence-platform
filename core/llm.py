"""
Shared LLM helper used by ingest_corpus/, core/resolution/, and core/probes/.

Wraps litellm.completion with the rate-limit retry policy already proven
in ingest_corpus/ingest.py, plus tolerant JSON parsing for responses
that arrive wrapped in ```json fences.
"""

import json
import os
import re

import litellm
import tenacity
from tqdm import tqdm

litellm.suppress_debug_info = True


# Langfuse tracing — enabled only when keys are present in the env so
# local dev without a Langfuse account doesn't error. Plan §3.3
# requires this for production observability + cost attribution; we're
# using it now to gather per-call cost data for the full-corpus
# estimate. Cloud Run prod can swap to a self-hosted Langfuse later
# by repointing LANGFUSE_HOST without touching app code.
if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]

# `LITELLM_MODEL` lets each environment route through a different
# provider without code changes. Useful for the Vertex-vs-direct-API
# split: Cloud Run prod can stay on Vertex (billing on the GCP
# project) while local dev points at Anthropic or OpenAI direct.
#
# Default = Vertex Gemini 2.5 Flash:
#   - Anthropic on Vertex was zero-quota in every region for this
#     project (commercial gate, not a region issue).
#   - Three-way audit (Sonnet 4.5 direct vs Gemini 2.5 Pro vs Flash)
#     showed Flash matched or beat Pro on every quality metric
#     (99% speaker attribution, 0 JSON parse errors, more claims)
#     at ~half the runtime and ~5-10× lower token cost.
DEFAULT_MODEL = os.environ.get(
    "LITELLM_MODEL", "vertex_ai/gemini-2.5-flash",
)


@tenacity.retry(
    retry=tenacity.retry_if_exception_type((
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.InternalServerError,
    )),
    wait=tenacity.wait_exponential(multiplier=2, min=10, max=120),
    stop=tenacity.stop_after_attempt(8),
    before_sleep=lambda rs: tqdm.write(
        f"  rate limit, retrying in {rs.next_action.sleep:.0f}s..."
    ),
)
def call_llm(model: str, messages: list, max_tokens: int = 4096) -> str:
    response = litellm.completion(model=model, max_tokens=max_tokens, messages=messages)
    return response.choices[0].message.content.strip()


def parse_json_response(raw: str):
    """Tolerant JSON parser for LLM output.

    Strict parse first; on failure, fall back to `json_repair` which
    handles common LLM malformations: truncation mid-string,
    unescaped inner quotes, missing closing braces, trailing commas.
    Recovers as much well-formed content as possible — for an array
    of objects truncated mid-record, you still get every complete
    object preceding the truncation point. The audit caught Gemini
    Pro hitting max_tokens mid-quote on long-feature articles; this
    salvages those calls instead of throwing them away.
    """
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(raw))
