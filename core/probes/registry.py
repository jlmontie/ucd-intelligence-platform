"""
Probe registry.

A probe definition lives as a single module under
core/probes/definitions/ that exports PROBE_SPEC. This module
imports them and exposes REGISTRY = {name: ProbeSpec}.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    version: int
    prompt: str
    schema_json: dict[str, Any]
    model: str | None = None  # None = caller picks


# Imported after ProbeSpec is defined so each definition module can
# `from core.probes.registry import ProbeSpec` without hitting a
# circular import on first load.
from core.probes.definitions import claims_v1, project_panel_v1, quotes_v1  # noqa: E402, I001


REGISTRY: dict[str, ProbeSpec] = {
    spec.name: spec
    for spec in (
        project_panel_v1.PROBE_SPEC,
        claims_v1.PROBE_SPEC,
        quotes_v1.PROBE_SPEC,
    )
}
