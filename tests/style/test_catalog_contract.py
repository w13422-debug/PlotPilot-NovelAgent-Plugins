from __future__ import annotations

import hashlib

from conftest import (
    MANUFACTURING_ROOT,
    ROOT,
    RUNTIME_ROOT,
    assert_recursively_closed,
    strict_json,
)
from jsonschema import Draft202012Validator

PLUGIN_EXPECTATIONS = {
    "com.plotpilot.novelagent.style-manufacturing": {
        "root": MANUFACTURING_ROOT,
        "needs": [
            "host.asset.read/v1",
            "host.asset.create/v1",
            "host.asset.upload.status/v1",
            "host.job.event/v1",
            "host.job.complete/v1",
            "host.checkpoint.commit/v1",
            "host.model.invoke/v1",
            "host.candidate.stage/v1",
        ],
        "capabilities": {
            "style.manufacture/v1": (
                "style.manufacture-request/v1",
                "style.manufacture-result/v1",
                "candidate-batch/v1",
                ["run", "resume", "cancel"],
                False,
                ["book-analysis-taxonomy/v1", "lexicon/v1"],
            ),
            "style.qualify/v1": (
                "style.qualify-request/v1",
                "style.qualify-result/v1",
                "diagnostic-bundle/v1",
                ["run", "resume", "cancel"],
                False,
                ["quality-rubric/v1"],
            ),
            "style.data-plugin.package/v1": (
                "style.data-plugin.package-request/v1",
                "style.data-plugin.package-result/v1",
                "artifact-bundle/v1",
                ["run", "validate"],
                True,
                ["style-pack/v1"],
            ),
        },
    },
    "com.plotpilot.novelagent.style-runtime": {
        "root": RUNTIME_ROOT,
        "needs": [
            "host.asset.read/v1",
            "host.asset.create/v1",
            "host.asset.upload.status/v1",
            "host.job.event/v1",
            "host.job.complete/v1",
            "host.checkpoint.commit/v1",
            "host.model.invoke/v1",
            "host.candidate.stage/v1",
        ],
        "capabilities": {
            "style.apply/v1": (
                "style.apply-request/v1",
                "style.apply-result/v1",
                "candidate-batch/v1",
                ["run", "resume", "cancel"],
                False,
                ["style-pack/v1", "lexicon/v1"],
            ),
            "style.review/v1": (
                "style.review-request/v1",
                "style.review-result/v1",
                "diagnostic-bundle/v1",
                ["run", "resume", "cancel"],
                False,
                ["style-pack/v1", "quality-rubric/v1"],
            ),
            "style.refine/v1": (
                "style.refine-request/v1",
                "style.refine-result/v1",
                "candidate-batch/v1",
                ["run", "resume", "cancel"],
                False,
                ["style-pack/v1", "quality-rubric/v1"],
            ),
        },
    },
}


def catalog_plugins() -> dict[str, dict]:
    catalog = strict_json(ROOT / "catalog" / "plugin-catalog-v1.json")
    return {item["plugin_id"]: item for item in catalog["code_plugins"]}


def test_manifests_and_runtime_descriptors_are_the_exact_frozen_catalog_projection() -> None:
    catalog = catalog_plugins()
    for plugin_id, expected in PLUGIN_EXPECTATIONS.items():
        root = expected["root"]
        manifest = strict_json(root / "plugin.json")
        assert manifest["plugin_id"] == plugin_id
        assert manifest["kind"] == "code"
        assert manifest["needs"] == expected["needs"]
        assert catalog[plugin_id]["needs"] == expected["needs"]
        actual = {item["capability_id"]: item for item in manifest["capabilities"]}
        assert set(actual) == set(expected["capabilities"])
        catalog_caps = {item["capability_id"]: item for item in catalog[plugin_id]["capabilities"]}
        package_expected = strict_json(root / "expected.json")
        descriptors = {
            value["capability_id"]: value
            for value in (
                strict_json(root / relative)
                for relative in package_expected["descriptor_paths"]
            )
        }
        for capability_id, frozen in expected["capabilities"].items():
            fields = (
                "input_schema",
                "output_schema",
                "result_contract",
                "operations",
                "deterministic",
                "accepted_data_formats",
            )
            expected_projection = dict(zip(fields, frozen, strict=True))
            assert actual[capability_id] == {
                "capability_id": capability_id,
                "operations": expected_projection["operations"],
                "result_contract": expected_projection["result_contract"],
            }
            descriptor_projection = {
                "input_schema": descriptors[capability_id]["input_schema"],
                "output_schema": descriptors[capability_id]["output_schema"],
                "result_contract": descriptors[capability_id]["result_contract"],
                "operations": descriptors[capability_id]["supports"],
                "deterministic": descriptors[capability_id]["deterministic"],
                "accepted_data_formats": descriptors[capability_id]["accepted_data_formats"],
            }
            assert descriptor_projection == expected_projection
            assert {field: catalog_caps[capability_id][field] for field in fields} == expected_projection


def test_catalog_format_interpreters_and_skill_entries_remain_frozen_without_public_edits() -> None:
    catalog = strict_json(ROOT / "catalog" / "plugin-catalog-v1.json")
    formats = {item["format_id"]: item for item in catalog["data_formats"]}
    assert formats["style-pack/v1"]["mergeable"] is False
    assert formats["lexicon/v1"]["mergeable"] is True
    assert {
        (item["plugin_id"], item["capability_id"])
        for item in formats["style-pack/v1"]["interpreters"]
        if item["plugin_id"].startswith("com.plotpilot.novelagent.style")
    } == {
        ("com.plotpilot.novelagent.style-manufacturing", "style.data-plugin.package/v1"),
        ("com.plotpilot.novelagent.style-runtime", "style.apply/v1"),
        ("com.plotpilot.novelagent.style-runtime", "style.review/v1"),
        ("com.plotpilot.novelagent.style-runtime", "style.refine/v1"),
    }
    assert {
        (item["plugin_id"], item["capability_id"])
        for item in formats["lexicon/v1"]["interpreters"]
        if item["plugin_id"].startswith("com.plotpilot.novelagent.style")
    } == {
        ("com.plotpilot.novelagent.style-manufacturing", "style.manufacture/v1"),
        ("com.plotpilot.novelagent.style-runtime", "style.apply/v1"),
    }
    skills = {item["skill_id"]: item for item in catalog["skills"]}
    assert skills["com.plotpilot.skill.donor.style-analysis"] == {
        "skill_id": "com.plotpilot.skill.donor.style-analysis",
        "stage": "analysis",
        "actions": ["analyze"],
        "runtime_plugin": "com.plotpilot.prompt-skill-runtime",
        "book_bound": False,
    }
    assert skills["com.plotpilot.skill.style.apply"] == {
        "skill_id": "com.plotpilot.skill.style.apply",
        "stage": "draft",
        "actions": ["draft", "refine"],
        "runtime_plugin": "com.plotpilot.prompt-skill-runtime",
        "book_bound": False,
    }


def test_every_style_schema_is_draft_2020_closed_hash_bound_and_rejects_unknown_fields() -> None:
    for root in (MANUFACTURING_ROOT, RUNTIME_ROOT):
        expected = strict_json(root / "expected.json")
        artifact_hashes = expected["schema_artifact_sha256"]
        actual = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.schema.json"))
        }
        assert actual == artifact_hashes
        for relative in actual:
            schema = strict_json(root / relative)
            Draft202012Validator.check_schema(schema)
            assert_recursively_closed(schema, relative)
        for relative in expected["schema_index_path_list"]:
            index = strict_json(root / relative)
            assert set(index) == {"schema", "plugin_id", "capability_id", "schemas"}
            assert index["plugin_id"] == strict_json(root / "plugin.json")["plugin_id"]
            assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected["schema_index_sha256"][relative]
            assert index["schemas"] == expected["schema_artifacts"][relative]


def test_public_contract_tree_is_not_used_as_a_dynamic_style_schema_authority() -> None:
    for root in (MANUFACTURING_ROOT, RUNTIME_ROOT):
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py")).lower()
        for forbidden in (
            "import sqlite3",
            "import requests",
            "import httpx",
            "import socket",
            "urlopen(",
            "plugins.style-",
            "plugins/style-",
            "catalog/plugin-catalog",
            "dynamic route",
        ):
            assert forbidden not in source
        other_module = "style_runtime" if root == MANUFACTURING_ROOT else "style_manufacturing"
        assert f"import {other_module}" not in source
        assert f"from {other_module}" not in source
