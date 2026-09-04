"""Canonical, release-neutral specification for narrative-analysis."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.narrative-analysis"
VERSION = "0.1.0"
NEEDS = (
    "host.asset.read/v1", "host.asset.create/v1", "host.asset.upload.status/v1",
    "host.job.event/v1", "host.job.complete/v1", "host.checkpoint.commit/v1",
    "host.model.invoke/v1", "host.candidate.stage/v1",
    "host.capability.invoke/v1", "host.capability.poll/v1", "host.capability.cancel/v1",
)

@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    descriptor_path: str
    schema_directory: str
    input_schema: str
    output_schema: str
    supports: tuple[str, ...]
    deterministic: bool
    accepted_data_formats: tuple[str, ...]
    ui_contributions: tuple[tuple[str, str], ...]
    result_contract: str = "candidate-batch/v1"
    bundle_type: str = "candidate_batch"

    @property
    def schema_index_path(self) -> str:
        return f"narrative_analysis/schemas/{self.schema_directory}/index.json"

    def descriptor(self, release_id: str) -> dict[str, Any]:
        return {
            "schema": "capability-provider/v1", "capability_id": self.capability_id,
            "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
            "input_schema": self.input_schema, "output_schema": self.output_schema,
            "result_contract": self.result_contract, "supports": list(self.supports),
            "deterministic": self.deterministic,
            "accepted_data_formats": list(self.accepted_data_formats),
        }

    def projection(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id, "descriptor_path": self.descriptor_path,
            "schema_index_path": self.schema_index_path, "input_schema": self.input_schema,
            "output_schema": self.output_schema, "result_contract": self.result_contract,
            "bundle_type": self.bundle_type, "supports": list(self.supports),
            "deterministic": self.deterministic,
            "accepted_data_formats": list(self.accepted_data_formats),
            "ui_contributions": [{"contribution_id": cid, "slot": slot} for cid, slot in self.ui_contributions],
        }

CAPABILITY_SPECS = (
    CapabilitySpec(
        "analysis.narrative.unit.extract/v1", "descriptor.json", "unit-extract",
        "analysis.narrative.unit.extract-request/v1", "analysis.narrative.unit.extract-result/v1",
        ("run", "resume", "cancel"), False,
        ("book-analysis-taxonomy/v1", "plot-structure-template/v1"),
        (("nap-ui-analysis-narrative-unit-extract-v1-donors-analysis-configure", "donors.analysis.configure"),),
    ),
    CapabilitySpec(
        "analysis.narrative.plan.compile/v1", "descriptor-plan-compile.json", "plan-compile",
        "analysis.narrative.plan.compile-request/v1", "analysis.narrative.plan.compile-result/v1",
        ("run", "validate"), True, ("plot-structure-template/v1",),
        (("nap-ui-analysis-narrative-plan-compile-v1-donors-analysis-configure", "donors.analysis.configure"),),
    ),
    CapabilitySpec(
        "analysis.narrative.synthesize/v1", "descriptor-synthesize.json", "synthesize",
        "analysis.narrative.synthesize-request/v1", "analysis.narrative.synthesize-result/v1",
        ("run", "resume", "cancel"), False, ("plot-structure-template/v1",),
        (
            ("nap-ui-analysis-narrative-synthesize-v1-donors-analysis-review", "donors.analysis.review"),
            ("nap-ui-analysis-narrative-synthesize-v1-outline-visualization-panel", "outline.visualization.panel"),
        ),
    ),
)
SPEC_BY_CAPABILITY = {spec.capability_id: spec for spec in CAPABILITY_SPECS}
CAPABILITIES = tuple(SPEC_BY_CAPABILITY)
DESCRIPTOR_PATHS = tuple(spec.descriptor_path for spec in CAPABILITY_SPECS)
INDEX_BY_CAPABILITY = {spec.capability_id: spec.schema_index_path for spec in CAPABILITY_SPECS}
if len(SPEC_BY_CAPABILITY) != len(CAPABILITY_SPECS):
    raise RuntimeError("duplicate narrative-analysis capability")

def capability_projection() -> list[dict[str, Any]]:
    return [spec.projection() for spec in CAPABILITY_SPECS]

def descriptors(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.capability_id: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}

def descriptor_files(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.descriptor_path: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}

def plugin_manifest() -> dict[str, Any]:
    return {
        "schema": "plotpilot-plugin/v1", "plugin_id": PLUGIN_ID, "version": VERSION,
        "display_name": "Novel-Agent 剧情结构分析",
        "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"},
        "capabilities": [{"capability_id": s.capability_id, "operations": list(s.supports), "result_contract": s.result_contract} for s in CAPABILITY_SPECS],
        "settings": None, "needs": list(NEEDS), "kind": "code",
        "backend": {"entrypoint": "narrative_analysis.runtime:main", "wheel": "backend/plotpilot_narrative_analysis-0.1.0-py3-none-any.whl", "requirements_lock": "backend/requirements.lock", "wheelhouse": "backend/wheels", "max_concurrency": 1},
        "storage": {"schema_version": 1, "migration_policy": "transactional-shadow", "migration_manifest": "migrations/manifest.json"},
        "ui": {"entry": "ui/metadata-only.json", "runtime": "worker-ui/v1", "contributions": [
            {"contribution_id": cid, "slot": slot, "capability_id": s.capability_id}
            for s in CAPABILITY_SPECS for cid, slot in s.ui_contributions
        ]}, "data": None,
    }

def ui_metadata() -> dict[str, Any]:
    slots: list[str] = []
    for spec in CAPABILITY_SPECS:
        for _cid, slot in spec.ui_contributions:
            if slot not in slots:
                slots.append(slot)
    return {"schema": "plugin-ui-metadata/v1", "plugin_id": PLUGIN_ID, "implementation": "host-projected-metadata-only", "slots": slots}

__all__ = ["CAPABILITIES", "CAPABILITY_SPECS", "DESCRIPTOR_PATHS", "INDEX_BY_CAPABILITY", "NEEDS", "PLUGIN_ID", "SPEC_BY_CAPABILITY", "VERSION", "capability_projection", "descriptor_files", "descriptors", "plugin_manifest", "ui_metadata"]
