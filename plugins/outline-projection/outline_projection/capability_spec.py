"""Frozen manifest, descriptor and UI projections for outline projection."""
from __future__ import annotations

PLUGIN_ID = "com.plotpilot.novelagent.outline-projection"
VERSION = "0.1.0"
CAPABILITY_ID = "outline.projection.render/v1"
INPUT_SCHEMA = "outline.projection.render-request/v1"
OUTPUT_SCHEMA = "outline.projection.render-result/v1"
RESULT_CONTRACT = "artifact-bundle/v1"
SUPPORTS = ["run", "cancel"]
NEEDS = [
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
]
CONTRIBUTIONS = [
    {
        "contribution_id": "nap-ui-outline-projection-render-v1-outline-visualization-panel",
        "slot": "outline.visualization.panel",
        "capability_id": CAPABILITY_ID,
    },
    {
        "contribution_id": "nap-ui-outline-projection-render-v1-donors-analysis-review",
        "slot": "donors.analysis.review",
        "capability_id": CAPABILITY_ID,
    },
]


def descriptor(release_id: str) -> dict[str, object]:
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "result_contract": RESULT_CONTRACT,
        "supports": SUPPORTS,
        "deterministic": True,
        "accepted_data_formats": [],
    }


def plugin_manifest() -> dict[str, object]:
    return {
        "schema": "plotpilot-plugin/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "display_name": "Novel-Agent 大纲投影",
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
            "python": "3.12.*",
        },
        "capabilities": [{
            "capability_id": CAPABILITY_ID,
            "operations": SUPPORTS,
            "result_contract": RESULT_CONTRACT,
        }],
        "settings": None,
        "needs": NEEDS,
        "kind": "code",
        "backend": {
            "entrypoint": "outline_projection.runtime:main",
            "wheel": "backend/plotpilot_outline_projection-0.1.0-py3-none-any.whl",
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
            "contributions": CONTRIBUTIONS,
        },
        "data": None,
    }


def ui_metadata() -> dict[str, object]:
    return {
        "schema": "plugin-ui-metadata/v1",
        "plugin_id": PLUGIN_ID,
        "implementation": "host-projected-metadata-only",
        "slots": ["outline.visualization.panel", "donors.analysis.review"],
    }


def capability_projection() -> list[dict[str, object]]:
    return [{
        "capability_id": CAPABILITY_ID,
        "descriptor_path": "descriptor.json",
        "schema_index_path": "outline_projection/schemas/render/index.json",
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "result_contract": RESULT_CONTRACT,
        "bundle_type": "artifact",
        "supports": SUPPORTS,
        "deterministic": True,
        "accepted_data_formats": [],
        "ui_contributions": [
            {"contribution_id": item["contribution_id"], "slot": item["slot"]}
            for item in CONTRIBUTIONS
        ],
    }]
