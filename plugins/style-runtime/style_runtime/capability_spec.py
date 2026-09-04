"""Release-neutral frozen catalog projection for the Style Runtime plugin."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.style-runtime"
VERSION = "0.1.0"

NEEDS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.model.invoke/v1",
    "host.candidate.stage/v1",
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
    accepted_data_formats: tuple[str, ...]
    ui_contributions: tuple[tuple[str, str], ...]

    @property
    def schema_index_path(self) -> str:
        return f"style_runtime/schemas/{self.schema_directory}/index.json"

    def manifest_capability(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "operations": list(self.supports),
            "result_contract": self.result_contract,
        }

    def descriptor(self, release_id: str) -> dict[str, Any]:
        return {
            "schema": "capability-provider/v1",
            "capability_id": self.capability_id,
            "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "result_contract": self.result_contract,
            "supports": list(self.supports),
            "deterministic": False,
            "accepted_data_formats": list(self.accepted_data_formats),
        }

    def release_neutral_projection(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "descriptor_path": self.descriptor_path,
            "schema_index_path": self.schema_index_path,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "result_contract": self.result_contract,
            "bundle_type": self.bundle_type,
            "supports": list(self.supports),
            "deterministic": False,
            "accepted_data_formats": list(self.accepted_data_formats),
            "ui_contributions": [
                {"contribution_id": contribution_id, "slot": slot}
                for contribution_id, slot in self.ui_contributions
            ],
        }


CAPABILITY_SPECS = (
    CapabilitySpec(
        "style.apply/v1", "descriptor.json", "apply",
        "style.apply-request/v1", "style.apply-result/v1",
        "candidate-batch/v1", "candidate_batch", ("run", "resume", "cancel"),
        ("style-pack/v1", "lexicon/v1"),
        (
            ("nap-ui-style-apply-v1-workbench-reference-panel", "workbench.reference.panel"),
            ("nap-ui-style-apply-v1-workbench-generate-context", "workbench.generate.context"),
        ),
    ),
    CapabilitySpec(
        "style.review/v1", "descriptor-review.json", "review",
        "style.review-request/v1", "style.review-result/v1",
        "diagnostic-bundle/v1", "diagnostic", ("run", "resume", "cancel"),
        ("style-pack/v1", "quality-rubric/v1"),
        (("nap-ui-style-review-v1-workbench-quality-panel", "workbench.quality.panel"),),
    ),
    CapabilitySpec(
        "style.refine/v1", "descriptor-refine.json", "refine",
        "style.refine-request/v1", "style.refine-result/v1",
        "candidate-batch/v1", "candidate_batch", ("run", "resume", "cancel"),
        ("style-pack/v1", "quality-rubric/v1"),
        (("nap-ui-style-refine-v1-workbench-quality-panel", "workbench.quality.panel"),),
    ),
)

SPEC_BY_CAPABILITY = {spec.capability_id: spec for spec in CAPABILITY_SPECS}
CAPABILITIES = tuple(SPEC_BY_CAPABILITY)
DESCRIPTOR_PATHS = tuple(spec.descriptor_path for spec in CAPABILITY_SPECS)
INDEX_BY_CAPABILITY = {spec.capability_id: spec.schema_index_path for spec in CAPABILITY_SPECS}

if len(SPEC_BY_CAPABILITY) != len(CAPABILITY_SPECS):
    raise RuntimeError("duplicate style-runtime capability")


def capability_projection() -> list[dict[str, Any]]:
    return [spec.release_neutral_projection() for spec in CAPABILITY_SPECS]


def descriptors(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.capability_id: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}


def descriptor_files(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.descriptor_path: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}


def plugin_manifest() -> dict[str, Any]:
    contributions = [
        {"contribution_id": contribution_id, "slot": slot, "capability_id": spec.capability_id}
        for spec in CAPABILITY_SPECS
        for contribution_id, slot in spec.ui_contributions
    ]
    return {
        "schema": "plotpilot-plugin/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "display_name": "Novel-Agent 文风应用",
        "compatibility": {
            "core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"
        },
        "capabilities": [spec.manifest_capability() for spec in CAPABILITY_SPECS],
        "settings": None,
        "needs": list(NEEDS),
        "kind": "code",
        "backend": {
            "entrypoint": "style_runtime.runtime:main",
            "wheel": "backend/plotpilot_style_runtime-0.1.0-py3-none-any.whl",
            "requirements_lock": "backend/requirements.lock",
            "wheelhouse": "backend/wheels",
            "max_concurrency": 1,
        },
        "storage": {
            "schema_version": 1,
            "migration_policy": "transactional-shadow",
            "migration_manifest": "migrations/manifest.json",
        },
        "ui": {"entry": "ui/metadata-only.json", "runtime": "worker-ui/v1", "contributions": contributions},
        "data": None,
    }


def ui_metadata() -> dict[str, Any]:
    slots: list[str] = []
    for spec in CAPABILITY_SPECS:
        for _contribution_id, slot in spec.ui_contributions:
            if slot not in slots:
                slots.append(slot)
    return {
        "schema": "plugin-ui-metadata/v1",
        "plugin_id": PLUGIN_ID,
        "implementation": "host-projected-metadata-only",
        "slots": slots,
    }
