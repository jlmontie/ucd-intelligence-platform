"""
Probe registry + runner.

A probe is a versioned (prompt, output schema) pair that runs against
a single article and writes its result to probe_runs. The runner
caches by (probe_id, article_id, probe_version, content_hash) so a
re-run is a no-op unless the article's text changed or the probe
version was bumped.

Decomposes the monolithic prompt that lived in
ingest_corpus/ingest.py:183 into three focused probes:

  project_panel_v1 — structured project facts + team rosters
  claims_v1        — notable factual assertions
  quotes_v1        — verbatim quotations with attribution

Each probe's definition lives in core/probes/definitions/ as a
PROBE_SPEC module attribute. The seed script (core/probes/seed.py)
upserts those definitions into the `probes` table so probe_runs has
an FK target.
"""

from core.probes.registry import REGISTRY, ProbeSpec
from core.probes.runner import run_probe_for_article, run_probes

__all__ = ["REGISTRY", "ProbeSpec", "run_probes", "run_probe_for_article"]
