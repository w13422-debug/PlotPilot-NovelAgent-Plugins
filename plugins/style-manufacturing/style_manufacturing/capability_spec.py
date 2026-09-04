"""Single-source frozen catalog projections for Style Manufacturing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.style-manufacturing"
VERSION = "0.1.0"
NEEDS = (
    "host.asset.read/v1", "host.asset.create/v1", "host.asset.upload.status/v1",
    "host.job.event/v1", "host.job.complete/v1", "host.checkpoint.commit/v1",
    "host.model.invoke/v1", "host.candidate.stage/v1",
)


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    descriptor_path: str
    schema_directory: str
    input_schema: str
    output_schema: str
    result_contract: str
    bundle_type: str
    supports: tuple[str, ...]
    deterministic: bool
    accepted_data_formats: tuple[str, ...]
    request_fields_group: str
    ui_contributions: tuple[tuple[str, str], ...]

    @property
    def schema_index_path(self) -> str:
        return f"style_manufacturing/schemas/{self.schema_directory}/index.json"

    def manifest_capability(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "operations": list(self.supports),
                "result_contract": self.result_contract}

    def descriptor(self, release_id: str) -> dict[str, Any]:
        return {"schema": "capability-provider/v1", "capability_id": self.capability_id,
                "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
                "input_schema": self.input_schema, "output_schema": self.output_schema,
                "result_contract": self.result_contract, "supports": list(self.supports),
                "deterministic": self.deterministic,
                "accepted_data_formats": list(self.accepted_data_formats)}

    def release_neutral_projection(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "descriptor_path": self.descriptor_path,
                "schema_index_path": self.schema_index_path, "input_schema": self.input_schema,
                "output_schema": self.output_schema, "result_contract": self.result_contract,
                "bundle_type": self.bundle_type, "supports": list(self.supports),
                "deterministic": self.deterministic, "request_fields_group": self.request_fields_group,
                "accepted_data_formats": list(self.accepted_data_formats),
                "ui_contributions": [{"contribution_id": i, "slot": s} for i, s in self.ui_contributions]}


CAPABILITY_SPECS = (
    CapabilitySpec("style.manufacture/v1", "descriptor.json", "manufacture",
                   "style.manufacture-request/v1", "style.manufacture-result/v1",
                   "candidate-batch/v1", "candidate_batch", ("run", "resume", "cancel"), False,
                   ("book-analysis-taxonomy/v1", "lexicon/v1"), "manufacture",
                   (("nap-ui-style-manufacture-v1-donors-analysis-configure", "donors.analysis.configure"),)),
    CapabilitySpec("style.qualify/v1", "descriptor-qualify.json", "qualify",
                   "style.qualify-request/v1", "style.qualify-result/v1",
                   "diagnostic-bundle/v1", "diagnostic", ("run", "resume", "cancel"), False,
                   ("quality-rubric/v1",), "qualify",
                   (("nap-ui-style-qualify-v1-donors-analysis-review", "donors.analysis.review"),)),
    CapabilitySpec("style.data-plugin.package/v1", "descriptor-data-package.json", "data-package",
                   "style.data-plugin.package-request/v1", "style.data-plugin.package-result/v1",
                   "artifact-bundle/v1", "artifact", ("run", "validate"), True,
                   ("style-pack/v1",), "package",
                   (("nap-ui-style-data-plugin-package-v1-donors-asset-derivation", "donors.asset.derivation"),)),
)
SPEC_BY_CAPABILITY = {s.capability_id: s for s in CAPABILITY_SPECS}
CAPABILITIES = tuple(SPEC_BY_CAPABILITY)
DESCRIPTOR_PATHS = tuple(s.descriptor_path for s in CAPABILITY_SPECS)
INDEX_BY_CAPABILITY = {s.capability_id: s.schema_index_path for s in CAPABILITY_SPECS}


def capability_projection() -> list[dict[str, Any]]:
    return [s.release_neutral_projection() for s in CAPABILITY_SPECS]


def descriptors(release_id: str) -> dict[str, dict[str, Any]]:
    return {s.capability_id: s.descriptor(release_id) for s in CAPABILITY_SPECS}


def descriptor_files(release_id: str) -> dict[str, dict[str, Any]]:
    return {s.descriptor_path: s.descriptor(release_id) for s in CAPABILITY_SPECS}


def plugin_manifest() -> dict[str, Any]:
    contributions = [{"contribution_id": i, "slot": slot, "capability_id": s.capability_id}
                     for s in CAPABILITY_SPECS for i, slot in s.ui_contributions]
    return {"schema": "plotpilot-plugin/v1", "plugin_id": PLUGIN_ID, "version": VERSION,
            "display_name": "Novel-Agent 文风制造与独立认证",
            "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"},
            "capabilities": [s.manifest_capability() for s in CAPABILITY_SPECS],
            "settings": None, "needs": list(NEEDS), "kind": "code",
            "backend": {"entrypoint": "style_manufacturing.runtime:main",
                        "wheel": "backend/plotpilot_style_manufacturing-0.1.0-py3-none-any.whl",
                        "requirements_lock": "backend/requirements.lock", "wheelhouse": "backend/wheels",
                        "max_concurrency": 1},
            "storage": {"schema_version": 1, "migration_policy": "transactional-shadow",
                        "migration_manifest": "migrations/manifest.json"},
            "ui": {"entry": "ui/metadata-only.json", "runtime": "worker-ui/v1", "contributions": contributions},
            "data": None}


def ui_metadata() -> dict[str, Any]:
    slots: list[str] = []
    for spec in CAPABILITY_SPECS:
        for _i, slot in spec.ui_contributions:
            if slot not in slots:
                slots.append(slot)
    return {"schema": "plugin-ui-metadata/v1", "plugin_id": PLUGIN_ID,
            "implementation": "host-projected-metadata-only", "slots": slots}
