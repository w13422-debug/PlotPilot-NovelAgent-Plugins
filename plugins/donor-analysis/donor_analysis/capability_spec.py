"""Canonical Generation-2 capability specification and package projections.

This module is deliberately release-neutral.  It is included in the wheel and
the outer package digest, while provider descriptors add the computed release
identifier only after that digest exists.  Runtime dispatch, generated package
metadata, and identity verification all consume these same immutable records.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLUGIN_ID = "com.plotpilot.novelagent.donor-analysis"
VERSION = "0.1.0"
ACCEPTED_DATA_FORMATS = ("book-analysis-taxonomy/v1",)

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
    deterministic: bool
    request_fields_group: str
    ui_contributions: tuple[tuple[str, str], ...]

    @property
    def schema_index_path(self) -> str:
        return f"donor_analysis/schemas/{self.schema_directory}/index.json"

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
            "deterministic": self.deterministic,
            "accepted_data_formats": list(ACCEPTED_DATA_FORMATS),
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
            "deterministic": self.deterministic,
            "request_fields_group": self.request_fields_group,
            "accepted_data_formats": list(ACCEPTED_DATA_FORMATS),
            "ui_contributions": [
                {"contribution_id": contribution_id, "slot": slot}
                for contribution_id, slot in self.ui_contributions
            ],
        }


CAPABILITY_SPECS = (
    CapabilitySpec(
        capability_id="analysis.book.atom.extract/v1",
        descriptor_path="descriptor.json",
        schema_directory="atom-extract",
        input_schema="analysis.book.atom.extract-request/v1",
        output_schema="analysis.book.atom.extract-result/v1",
        result_contract="candidate-batch/v1",
        bundle_type="candidate_batch",
        supports=("run", "resume", "cancel"),
        deterministic=False,
        request_fields_group="source",
        ui_contributions=(
            ("nap-ui-analysis-book-atom-extract-v1-donors-analysis-configure", "donors.analysis.configure"),
            ("nap-ui-analysis-book-atom-extract-v1-job-drawer-detail", "job.drawer.detail"),
        ),
    ),
    CapabilitySpec(
        capability_id="analysis.book.atom.manual/v1",
        descriptor_path="descriptor-atom-manual.json",
        schema_directory="atom-manual",
        input_schema="analysis.book.atom.manual-request/v1",
        output_schema="analysis.book.atom.manual-result/v1",
        result_contract="candidate-batch/v1",
        bundle_type="candidate_batch",
        supports=("run", "validate"),
        deterministic=True,
        request_fields_group="manual",
        ui_contributions=(
            ("nap-ui-analysis-book-atom-manual-v1-donors-analysis-review", "donors.analysis.review"),
        ),
    ),
    CapabilitySpec(
        capability_id="analysis.book.claim.generate/v1",
        descriptor_path="descriptor-claim-generate.json",
        schema_directory="claim-generate",
        input_schema="analysis.book.claim.generate-request/v1",
        output_schema="analysis.book.claim.generate-result/v1",
        result_contract="candidate-batch/v1",
        bundle_type="candidate_batch",
        supports=("run", "resume", "cancel"),
        deterministic=False,
        request_fields_group="claim",
        ui_contributions=(
            ("nap-ui-analysis-book-claim-generate-v1-donors-analysis-review", "donors.analysis.review"),
        ),
    ),
    CapabilitySpec(
        capability_id="analysis.book.rereview/v1",
        descriptor_path="descriptor-rereview.json",
        schema_directory="rereview",
        input_schema="analysis.book.rereview-request/v1",
        output_schema="analysis.book.rereview-result/v1",
        result_contract="diagnostic-bundle/v1",
        bundle_type="diagnostic",
        supports=("run", "resume", "cancel"),
        deterministic=False,
        request_fields_group="rereview",
        ui_contributions=(
            ("nap-ui-analysis-book-rereview-v1-donors-analysis-review", "donors.analysis.review"),
            ("nap-ui-analysis-book-rereview-v1-donors-evidence-inspector", "donors.evidence.inspector"),
        ),
    ),
)

SPEC_BY_CAPABILITY = {spec.capability_id: spec for spec in CAPABILITY_SPECS}
CAPABILITIES = tuple(SPEC_BY_CAPABILITY)
DESCRIPTOR_PATHS = tuple(spec.descriptor_path for spec in CAPABILITY_SPECS)
INDEX_BY_CAPABILITY = {
    spec.capability_id: spec.schema_index_path for spec in CAPABILITY_SPECS
}

if len(SPEC_BY_CAPABILITY) != len(CAPABILITY_SPECS):
    raise RuntimeError("duplicate donor-analysis capability specification")
if len(set(DESCRIPTOR_PATHS)) != len(DESCRIPTOR_PATHS):
    raise RuntimeError("duplicate donor-analysis descriptor path")


def capability_projection() -> list[dict[str, Any]]:
    """Return the canonical release-neutral semantics bound by package identity."""
    return [spec.release_neutral_projection() for spec in CAPABILITY_SPECS]


def descriptors(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.capability_id: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}


def descriptor_files(release_id: str) -> dict[str, dict[str, Any]]:
    return {spec.descriptor_path: spec.descriptor(release_id) for spec in CAPABILITY_SPECS}


def plugin_manifest() -> dict[str, Any]:
    contributions = [
        {
            "contribution_id": contribution_id,
            "slot": slot,
            "capability_id": spec.capability_id,
        }
        for spec in CAPABILITY_SPECS
        for contribution_id, slot in spec.ui_contributions
    ]
    return {
        "schema": "plotpilot-plugin/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "display_name": "Novel-Agent 原子拆书与证据复核",
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
            "python": "3.12.*",
        },
        "capabilities": [spec.manifest_capability() for spec in CAPABILITY_SPECS],
        "settings": None,
        "needs": list(NEEDS),
        "kind": "code",
        "backend": {
            "entrypoint": "donor_analysis.runtime:main",
            "wheel": "backend/plotpilot_donor_analysis-0.1.0-py3-none-any.whl",
            "requirements_lock": "backend/requirements.lock",
            "wheelhouse": "backend/wheels",
            "max_concurrency": 1,
        },
        "storage": {
            "schema_version": 1,
            "migration_policy": "transactional-shadow",
            "migration_manifest": "migrations/manifest.json",
        },
        "ui": {
            "entry": "ui/metadata-only.json",
            "runtime": "worker-ui/v1",
            "contributions": contributions,
        },
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


__all__ = [
    "ACCEPTED_DATA_FORMATS",
    "CAPABILITIES",
    "CAPABILITY_SPECS",
    "DESCRIPTOR_PATHS",
    "INDEX_BY_CAPABILITY",
    "NEEDS",
    "PLUGIN_ID",
    "SPEC_BY_CAPABILITY",
    "VERSION",
    "CapabilitySpec",
    "capability_projection",
    "descriptor_files",
    "descriptors",
    "plugin_manifest",
    "ui_metadata",
]
