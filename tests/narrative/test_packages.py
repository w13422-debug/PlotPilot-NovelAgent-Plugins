from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest
from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.package import (  # noqa: E402
    digest_package,
    skill_package_hash,
    skill_release_id,
)
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    assert_valid,
    verify_data_bundle,
    verify_manifest,
)


CODE_PACKAGES = {
    "narrative-analysis": {
        "plugin_id": "com.plotpilot.novelagent.narrative-analysis",
        "capabilities": {
            "analysis.narrative.unit.extract/v1": (
                ("run", "resume", "cancel"), "candidate-batch/v1", False,
                ("book-analysis-taxonomy/v1", "plot-structure-template/v1"),
            ),
            "analysis.narrative.plan.compile/v1": (
                ("run", "validate"), "candidate-batch/v1", True,
                ("plot-structure-template/v1",),
            ),
            "analysis.narrative.synthesize/v1": (
                ("run", "resume", "cancel"), "candidate-batch/v1", False,
                ("plot-structure-template/v1",),
            ),
        },
    },
    "outline-projection": {
        "plugin_id": "com.plotpilot.novelagent.outline-projection",
        "capabilities": {
            "outline.projection.render/v1": (
                ("run", "cancel"), "artifact-bundle/v1", True, (),
            ),
        },
    },
}

NAP03_PLUGIN_IDS = frozenset({
    "com.plotpilot.novelagent.narrative-analysis",
    "com.plotpilot.novelagent.outline-projection",
})

SKILLS = {
    "narrative-unit": ("com.plotpilot.skill.donor.narrative-unit", "analysis", ("extract",), ("analysis.narrative.unit.extract/v1",)),
    "book": ("com.plotpilot.skill.outline.book", "outline", ("plan",), ("analysis.narrative.plan.compile/v1",)),
    "volume": ("com.plotpilot.skill.outline.volume", "outline", ("plan",), ("analysis.narrative.plan.compile/v1",)),
    "chapter": ("com.plotpilot.skill.outline.chapter", "outline", ("plan",), ("analysis.narrative.plan.compile/v1",)),
    "plot-unit": ("com.plotpilot.skill.outline.plot-unit", "outline", ("plan",), ("analysis.narrative.plan.compile/v1",)),
}

FORMAT_CAPABILITIES = (
    "analysis.narrative.unit.extract/v1",
    "analysis.narrative.plan.compile/v1",
    "analysis.narrative.synthesize/v1",
    "asset.template.derive/v1",
    "writing.context.assemble/v1",
    "writing.project.outline/v1",
    "writing.volume.outline/v1",
    "writing.chapter.outline/v1",
)


def strict_json(path: Path) -> dict:
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = child
        return value

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def manifest_files(package_root: Path) -> dict[str, bytes]:
    lines = (package_root / "files.sha256").read_text(encoding="utf-8").splitlines()
    files: dict[str, bytes] = {}
    for line in lines:
        digest, relative = line.split("  ", 1)
        raw = (package_root / relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        files[relative] = raw
    return files


def assert_recursively_closed(schema, path: str = "$") -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, path
        for key, value in schema.items():
            assert_recursively_closed(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            assert_recursively_closed(value, f"{path}[{index}]")


@pytest.mark.parametrize("directory", CODE_PACKAGES)
def test_code_manifest_descriptor_schema_index_and_identity_are_bidirectionally_closed(directory: str) -> None:
    root = ROOT / "plugins" / directory
    contract = CODE_PACKAGES[directory]
    manifest = strict_json(root / "plugin.json")
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / expected["identity_path"])
    verify_manifest(manifest)
    assert manifest["plugin_id"] == expected["plugin_id"] == contract["plugin_id"]
    digest = digest_package(manifest_files(root), manifest["plugin_id"], manifest["version"])
    assert digest.files_sha256 == (root / "files.sha256").read_bytes()
    assert (digest.package_hash, digest.release_id) == (expected["package_hash"], expected["release_id"])
    assert identity["package_hash"] == digest.package_hash
    assert identity["release_id"] == digest.release_id

    manifest_caps = {item["capability_id"]: item for item in manifest["capabilities"]}
    assert set(manifest_caps) == set(contract["capabilities"])
    assert set(expected["capability_projection"][i]["capability_id"] for i in range(len(expected["capability_projection"]))) == set(manifest_caps)
    for projection in expected["capability_projection"]:
        capability = projection["capability_id"]
        supports, result_contract, deterministic, formats = contract["capabilities"][capability]
        assert tuple(projection["supports"]) == supports
        assert projection["result_contract"] == result_contract
        assert projection["deterministic"] is deterministic
        assert tuple(projection["accepted_data_formats"]) == formats
        assert manifest_caps[capability]["operations"] == list(supports)
        assert manifest_caps[capability]["result_contract"] == result_contract
        descriptor = strict_json(root / projection["descriptor_path"])
        assert descriptor["capability_id"] == capability
        assert descriptor["provider"] == {"plugin_id": manifest["plugin_id"], "release_id": digest.release_id}
        assert tuple(descriptor["supports"]) == supports
        assert descriptor["result_contract"] == result_contract
        assert descriptor["deterministic"] is deterministic
        assert tuple(descriptor["accepted_data_formats"]) == formats
        index_path = root / projection["schema_index_path"]
        index = strict_json(index_path)
        assert index["plugin_id"] == manifest["plugin_id"]
        assert index["capability_id"] == capability
        assert {item["schema_id"] for item in index["schemas"]} == {
            projection["input_schema"], projection["output_schema"]
        }
        schema_root = index_path.parents[2]
        for item in index["schemas"]:
            schema_path = (schema_root / item["path"]).resolve()
            assert schema_path.is_relative_to(root.resolve())
            raw = schema_path.read_bytes()
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
            schema = strict_json(schema_path)
            # The provider index is an identity boundary: its schema_id must
            # be the exact document $id, not merely a matching set member.
            assert item["schema_id"] == schema.get("$id")
            assert item["sha256"] == hashlib.sha256(raw).hexdigest()
            Draft202012Validator.check_schema(schema)
            assert_recursively_closed(schema)


def test_frozen_catalog_plugin_capability_and_display_identity_is_bidirectional() -> None:
    """The source manifests must be an exact projection of the frozen catalog.

    This deliberately reads the repository catalog rather than repeating a
    hand-written list of names in the test.  It catches both directions of
    drift: a package missing from the frozen NAP-03 set and a package or
    capability claiming metadata that the catalog does not authorize.
    """
    catalog = strict_json(ROOT / "catalog" / "plugin-catalog-v1.json")
    entries = {
        item["plugin_id"]: item
        for item in catalog["code_plugins"]
        if item.get("project") == "NAP-03"
    }
    assert set(entries) == set(NAP03_PLUGIN_IDS)
    package_ids = {
        strict_json(ROOT / "plugins" / directory / "plugin.json")["plugin_id"]
        for directory in CODE_PACKAGES
    }
    assert package_ids == set(NAP03_PLUGIN_IDS)
    for directory in CODE_PACKAGES:
        manifest = strict_json(ROOT / "plugins" / directory / "plugin.json")
        frozen = entries[manifest["plugin_id"]]
        assert manifest["display_name"] == frozen["display_name"]
        expected_caps = [
            {
                "capability_id": item["capability_id"],
                "operations": item["operations"],
                "result_contract": item["result_contract"],
            }
            for item in frozen["capabilities"]
        ]
        assert manifest["capabilities"] == expected_caps
        expected_ui = [
            contribution
            for item in frozen["capabilities"]
            for contribution in item["ui_contributions"]
        ]
        assert manifest["ui"]["contributions"] == expected_ui
        assert manifest["ui"]["entry"] == "ui/metadata-only.json"


def test_provider_schema_indexes_and_manifests_close_over_exact_git_blobs() -> None:
    """Schema identity sidecars must describe the committed bytes, both ways."""
    def git_blob(relative: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "blob", f"HEAD:{relative}"],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        return result.stdout

    for directory in CODE_PACKAGES:
        package = ROOT / "plugins" / directory
        expected = strict_json(package / "expected.json")
        manifest_path = package / "files.sha256"
        manifest_blob = git_blob(manifest_path.relative_to(ROOT).as_posix())
        assert manifest_blob == manifest_path.read_bytes()
        manifest = {}
        for line in manifest_blob.decode("utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            manifest[relative] = digest

        indexed_schema_paths: set[str] = set()
        for index_relative in expected["schema_index_path_list"]:
            index_path = package / index_relative
            index_repo_path = index_path.relative_to(ROOT).as_posix()
            index_bytes = index_path.read_bytes()
            assert git_blob(index_repo_path) == index_bytes
            assert manifest[index_repo_path.removeprefix(f"plugins/{directory}/")] == hashlib.sha256(index_bytes).hexdigest()
            index = strict_json(index_path)
            for item in index["schemas"]:
                schema_path = package / directory.replace("-", "_") / item["path"]
                schema_repo_path = schema_path.relative_to(ROOT).as_posix()
                schema_bytes = schema_path.read_bytes()
                assert git_blob(schema_repo_path) == schema_bytes
                assert item["schema_id"] == strict_json(schema_path).get("$id")
                assert item["sha256"] == hashlib.sha256(schema_bytes).hexdigest()
                assert manifest[schema_repo_path.removeprefix(f"plugins/{directory}/")] == item["sha256"]
                indexed_schema_paths.add(schema_repo_path)

        physical_schema_paths = {
            path.relative_to(ROOT).as_posix()
            for path in (package / directory.replace("-", "_") / "schemas").rglob("*.schema.json")
        }
        assert indexed_schema_paths <= physical_schema_paths


@pytest.mark.parametrize("directory", SKILLS)
def test_all_five_skill_packages_are_immutable_closed_and_separately_identified(directory: str) -> None:
    root = ROOT / "skills" / "outline" / directory
    skill_id, stage, actions, capabilities = SKILLS[directory]
    manifest = strict_json(root / "skill.json")
    method = strict_json(root / "method.json")
    schema = strict_json(root / "method.schema.json")
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / "identity.json")
    assert_valid("plotpilot-skill/v1", manifest)
    assert manifest["skill_id"] == method["skill_id"] == skill_id
    assert manifest["stage"] == stage
    assert tuple(manifest["actions"]) == actions
    assert tuple(method["capability_ids"]) == capabilities
    assert method["input_format_id"] == "plot-structure-template/v1"
    assert method["result_contract"] == "candidate-batch/v1"
    assert method["authority"] == "candidate_only"
    assert method["immutable"] is True
    Draft202012Validator.check_schema(schema)
    assert_recursively_closed(schema)
    Draft202012Validator(schema).validate(method)
    broken = deepcopy(method)
    broken["second_authority"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(broken)
    files = manifest_files(root)
    digest = skill_package_hash(files)
    release = skill_release_id(skill_id, manifest["version"], digest)
    assert (digest, release) == (expected["skill_package_hash"], expected["skill_release_id"])
    assert identity["skill_package_hash"] == digest
    assert identity["skill_release_id"] == release


def test_plot_structure_data_package_identity_schema_and_catalog_interpreters() -> None:
    root = ROOT / "data" / "plot-structure" / "v1"
    manifest = strict_json(root / "plugin.json")
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / "identity.json")
    templates = strict_json(root / "data" / "templates.json")
    verify_manifest(manifest)
    assert manifest["kind"] == "data"
    assert manifest["data"] == {"format": "plot-structure-template/v1", "root": "data/templates.json"}
    digest = digest_package(manifest_files(root), manifest["plugin_id"], manifest["version"])
    assert (digest.package_hash, digest.release_id) == (expected["package_hash"], expected["release_id"])
    assert identity["package_hash"] == digest.package_hash
    assert identity["release_id"] == digest.release_id
    assert templates["schema"] == templates["format_id"] == "plot-structure-template/v1"
    assert templates["mergeable"] is False
    observed = tuple(
        (item["capability_id"], item["direction"])
        for item in templates["interpreter_mappings"]
    )
    assert observed == tuple(
        (capability, direction)
        for capability in FORMAT_CAPABILITIES
        for direction in ("format_to_capability", "capability_to_format")
    )
    bundle = strict_json(root / "fixtures" / "plugin-data-bundle.json")
    verify_data_bundle(bundle)
    assert bundle["data_release_id"] == digest.release_id
    assert bundle["package_hash"] == digest.package_hash
    for relative in (
        "schemas/plot-structure-template-v1.schema.json",
        "schemas/interpreter-roundtrip-v1.schema.json",
    ):
        schema = strict_json(root / relative)
        Draft202012Validator.check_schema(schema)
        assert_recursively_closed(schema)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def test_two_complete_rebuild_rounds_are_byte_identical(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    shutil.copytree(ROOT / "sdk", repository / "sdk")
    shutil.copytree(ROOT / "contracts", repository / "contracts")
    for directory in CODE_PACKAGES:
        shutil.copytree(ROOT / "plugins" / directory, repository / "plugins" / directory)
    shutil.copytree(ROOT / "skills" / "outline", repository / "skills" / "outline")
    shutil.copytree(ROOT / "data" / "plot-structure", repository / "data" / "plot-structure")
    roots = [
        repository / "plugins" / "narrative-analysis",
        repository / "plugins" / "outline-projection",
        repository / "skills" / "outline",
        repository / "data" / "plot-structure",
    ]
    baseline = {root: snapshot(root) for root in roots}
    commands = [
        [sys.executable, str(roots[0] / "rebuild_package.py"), "--rebuild"],
        [sys.executable, str(roots[1] / "rebuild_package.py"), "--rebuild"],
        [sys.executable, str(roots[2] / "rebuild_packages.py"), "--rebuild"],
        [sys.executable, str(roots[3] / "rebuild_package.py"), "--rebuild"],
    ]
    # Prove that the committed generated identities are recoverable rather
    # than merely re-checked: remove one generated Skill sidecar before the
    # first round and require the rebuild to restore byte-identical output.
    (repository / "skills" / "outline" / "book" / "identity.json").unlink()
    for _ in range(2):
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            assert completed.stdout.strip()
        assert {root: snapshot(root) for root in roots} == baseline


def test_package_manifests_exclude_python_and_pytest_caches() -> None:
    expected_files = []
    for directory in CODE_PACKAGES:
        expected_files.extend(strict_json(ROOT / "plugins" / directory / "expected.json")["package_files"])
    for directory in SKILLS:
        expected_files.extend(strict_json(ROOT / "skills" / "outline" / directory / "expected.json")["package_files"])
    expected_files.extend(strict_json(ROOT / "data" / "plot-structure" / "v1" / "expected.json")["package_files"])
    assert all("__pycache__" not in item and not item.endswith((".pyc", ".pyo")) for item in expected_files)



def _valid_result_bundle(contract_id: str = "artifact-bundle/v1") -> dict:
    """Build the smallest closed Result Bundle accepted by the output schemas."""
    h = "a" * 64
    producer = {
        "plugin_id": "com.plotpilot.test",
        "release_id": h,
        "capability_id": "outline.projection.render/v1",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
    }
    source_ref = {
        "workspace_id": "ws-1",
        "source_type": "asset",
        "source_id": "asset-source",
        "revision_or_hash": h,
    }
    artifact = {
        "schema": "artifact-item/v1",
        "item_id": "artifact-1",
        "artifact_kind": "outline.read-only-projection/v1",
        "payload_asset_id": "asset-payload",
        "payload_hash": h,
        "mime": "application/json",
        "source_refs": [source_ref],
        "status": "complete",
    }
    diagnostic = {
        "schema": "diagnostic-item/v1",
        "item_id": "diagnostic-1",
        "severity": "error",
        "code": "INVALID_SOURCE",
        "message": "invalid source",
        "details_asset_id": "asset-detail",
        "details_hash": h,
        "source_refs": [],
        "status": "failed",
    }
    candidate = {
        "schema": "candidate-item/v1",
        "item_id": "candidate-1",
        "item_kind": "relation_set",
        "target": {"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "relation-1"},
        "mutation": {"mode": "relation_patch", "payload_schema": "narrative-unit/v1", "payload_hash": h},
        "payload_asset_id": "asset-payload",
        "base": {"revision_id": "rev-1", "content_hash": h},
        "write_set": [{"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "relation-1", "revision_id": "rev-1", "content_hash": h}],
        "parent_candidate_ids": [],
        "source_refs": [source_ref],
        "status": "complete",
    }
    if contract_id == "artifact-bundle/v1":
        bundle_type, items, partial = "artifact", [artifact], False
    elif contract_id == "diagnostic-bundle/v1":
        bundle_type, items, partial = "diagnostic", [diagnostic], True
    else:
        bundle_type, items, partial = "candidate_batch", [candidate], False
    return {
        "schema": "result-bundle/v1",
        "contract_id": contract_id,
        "bundle_id": "bundle-1",
        "bundle_type": bundle_type,
        "producer": producer,
        "input_snapshot_hash": h,
        "items": items,
        "warnings": [],
        "partial": partial,
        "provenance_receipt_id": "receipt-1",
        "skill_chain_result_refs": [],
    }


def _valid_outline_projection() -> dict:
    synthesis = strict_json(ROOT / "plugins" / "narrative-analysis" / "fixtures" / "narrative-synthesis.json")
    span = synthesis["evidence_spans"][0]
    attr = synthesis["source_attributions"][0]
    child = synthesis["broker_children"][0]
    unit_id = "unit-fixture-1"
    node = {
        "node_id": "book-1",
        "parent_node_id": None,
        "level": "book",
        "title": "Book",
        "unit_ids": [unit_id],
        "order": 0,
        "evidence_span_ids": [span["evidence_span_id"]],
        "source_attribution_ids": [attr["attribution_id"]],
    }
    card = {
        "unit_id": unit_id,
        "unit_kind": "turn",
        "title": "转折",
        "summary": "摘要",
        "order": 0,
        "evidence_span_ids": [span["evidence_span_id"]],
        "source_attribution_ids": [attr["attribution_id"]],
    }
    timeline = {"event_id": "timeline-unit-fixture-1", "sequence": 0, **card}
    relation = {
        "relation_id": "relation-1",
        "source_unit_id": unit_id,
        "relation_type": "precedes",
        "target_unit_id": "unit-fixture-2",
        "evidence_span_ids": [span["evidence_span_id"]],
        "source_attribution_ids": [attr["attribution_id"]],
    }
    return {
        "schema": "outline-projection/v1",
        "projection_id": "projection-1",
        "source": {
            "asset_id": "asset-synthesis",
            "asset_hash": "a" * 64,
            "schema": "narrative-synthesis/v1",
            "synthesis_id": synthesis["synthesis_id"],
            "plan_id": synthesis["plan_ref"]["plan_id"],
        },
        "views": [
            {"mode": "tree", "payload": {"schema": "outline-tree-projection/v1", "nodes": [node]}},
            {"mode": "card", "payload": {"schema": "outline-card-projection/v1", "cards": [card]}},
            {"mode": "timeline", "payload": {"schema": "outline-timeline-projection/v1", "events": [timeline]}},
            {"mode": "relation", "payload": {"schema": "outline-relation-projection/v1", "relations": []}},
        ],
        "evidence_spans": [span],
        "source_attributions": [attr],
        "broker_children": [child],
        "authority": {
            "mode": "read_only_projection",
            "authoritative_source": "narrative-synthesis/v1",
            "creates_second_authority": False,
            "free_form_canvas": False,
        },
    }


def test_schema_negative_matrix_covers_warning_chain_view_and_authority_profiles() -> None:
    """Every frozen malformed-shape class must fail the committed schemas."""
    h = "a" * 64
    business = {
        "narrative-unit.schema.json": ("narrative-unit.json", (("summary", 42), ("unexpected", True))),
        "narrative-plan.schema.json": ("narrative-plan.json", (("immutable", False), ("hierarchy", [{"unexpected": True}]))),
        "narrative-synthesis.schema.json": ("narrative-synthesis.json", (("source_attributions", [{"unexpected": True}]), ("broker_children", [{"binding_id": True}]))),
    }
    for schema_name, (fixture_name, mutations) in business.items():
        schema = strict_json(ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "schemas" / schema_name)
        validator = Draft202012Validator(schema)
        baseline = strict_json(ROOT / "plugins" / "narrative-analysis" / "fixtures" / fixture_name)
        validator.validate(baseline)
        for field, bad in mutations:
            broken = deepcopy(baseline)
            broken[field] = bad
            with pytest.raises(ValidationError):
                validator.validate(broken)

    projection_schema = strict_json(ROOT / "plugins" / "outline-projection" / "outline_projection" / "schemas" / "outline-projection.schema.json")
    projection_validator = Draft202012Validator(projection_schema)
    projection = _valid_outline_projection()
    projection_validator.validate(projection)
    projection_cases = []
    broken = deepcopy(projection)
    broken["views"][0]["payload"]["schema"] = "outline-card-projection/v1"
    projection_cases.append(broken)  # mode/payload oneOf mismatch
    for field in ("free_form_canvas", "creates_second_authority"):
        broken = deepcopy(projection)
        broken["authority"][field] = True
        projection_cases.append(broken)
    broken = deepcopy(projection)
    broken["evidence_spans"][0]["start_codepoint"] = True
    projection_cases.append(broken)
    broken = deepcopy(projection)
    broken["source"]["unknown"] = 1
    projection_cases.append(broken)
    for broken in projection_cases:
        with pytest.raises(ValidationError):
            projection_validator.validate(broken)

    result_paths = [
        ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "schemas" / "unit-extract" / "result.schema.json",
        ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "schemas" / "plan-compile" / "result.schema.json",
        ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "schemas" / "synthesize" / "result.schema.json",
        ROOT / "plugins" / "outline-projection" / "outline_projection" / "schemas" / "render" / "result.schema.json",
    ]
    for path in result_paths:
        validator = Draft202012Validator(strict_json(path))
        baseline = _valid_result_bundle("artifact-bundle/v1")
        validator.validate(baseline)
        cases = []
        broken = deepcopy(baseline)
        broken["warnings"] = [42]
        cases.append(broken)
        broken = deepcopy(baseline)
        broken["warnings"] = [{"code": "x", "message": "", "details_asset_id": None, "unknown": True}]
        cases.append(broken)
        chain = {"schema": "skill-chain-ref/v1", "chain_result_id": "chain-1", "asset_id": None, "asset_hash": None, "result_bundle_id": "bundle-1", "result_item_id": "artifact-1", "stream_id": "stream-1", "acked_prefix_hash": h}
        broken = deepcopy(baseline)
        broken["skill_chain_result_refs"] = [chain]
        cases.append(broken)  # double anchor
        chain = {**chain, "result_bundle_id": None, "result_item_id": None, "stream_id": None, "acked_prefix_hash": None}
        broken = deepcopy(baseline)
        broken["skill_chain_result_refs"] = [chain]
        cases.append(broken)  # bundleless
        chain = {**chain, "asset_hash": True}
        broken = deepcopy(baseline)
        broken["skill_chain_result_refs"] = [chain]
        cases.append(broken)  # wrong type
        for broken in cases:
            with pytest.raises(ValidationError):
                validator.validate(broken)

    data_schema = strict_json(ROOT / "data" / "plot-structure" / "v1" / "schemas" / "plot-structure-template-v1.schema.json")
    data_validator = Draft202012Validator(data_schema)
    data_value = strict_json(ROOT / "data" / "plot-structure" / "v1" / "data" / "templates.json")
    data_validator.validate(data_value)
    for field, bad in (("mergeable", True), ("unexpected", True)):
        broken = deepcopy(data_value)
        broken[field] = bad
        with pytest.raises(ValidationError):
            data_validator.validate(broken)
    broken = deepcopy(data_value)
    broken["templates"][0]["beats"][0]["required"] = 1
    with pytest.raises(ValidationError):
        data_validator.validate(broken)


@pytest.mark.parametrize("directory,module", [("narrative-analysis", "narrative_analysis"), ("outline-projection", "outline_projection")])
def test_installed_wheel_replays_identity_without_source_checkout(directory: str, module: str) -> None:
    """The wheel carries its own sidecars and fails closed on sidecar tampering."""
    wheel = next((ROOT / "plugins" / directory / "backend").glob("*.whl"))
    with tempfile.TemporaryDirectory(prefix="nap03-install-") as temp:
        target = Path(temp) / "install"
        install = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(target), str(wheel)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert install.returncode == 0, install.stdout + install.stderr
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SDK_ROOT) + os.pathsep + str(target)
        probe = subprocess.run(
            [sys.executable, "-c", f"import {module}.runtime as runtime; print(runtime.PACKAGE_HASH); print(runtime.RELEASE_ID)"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert len(probe.stdout.splitlines()) == 2
        for mode in ("missing", "malformed", "tampered"):
            broken = Path(temp) / mode
            shutil.copytree(target, broken)
            if mode == "missing":
                (broken / "files.sha256").unlink()
            elif mode == "malformed":
                (broken / "files.sha256").write_text("not-a-manifest\n", encoding="utf-8")
            else:
                identity_path = broken / module / "identity.json"
                identity = strict_json(identity_path)
                identity["package_hash"] = "0" * 64
                identity_path.write_text(json.dumps(identity), encoding="utf-8")
            broken_env = os.environ.copy()
            broken_env["PYTHONPATH"] = str(SDK_ROOT) + os.pathsep + str(broken)
            rejected = subprocess.run(
                [sys.executable, "-c", f"import {module}.runtime"],
                env=broken_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            assert rejected.returncode != 0, mode
