from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.package import (
    digest_package,
    skill_package_hash,
    skill_release_id,
)
from plotpilot_plugin_sdk.verifier import (
    assert_valid,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_skill_identity,
)

DATA_ROOT = ROOT / "data" / "book-analysis-taxonomy" / "v1"
SKILLS_ROOT = ROOT / "skills" / "donor-analysis"
CAPABILITY_ORDER = (
    "analysis.book.atom.extract/v1",
    "analysis.book.atom.manual/v1",
    "analysis.book.claim.generate/v1",
    "analysis.book.rereview/v1",
)
FROZEN_IDENTITIES = {
    "data": (
        "bc00ec1e98ed00f944372546ad410feaaa771040bd0637abbe53c738f61f9e25",
        "02a807a6026e127d1c6f543c65d71f02899299ef1a5e3746ccdc8b3ecd5b758f",
    ),
    "atomic-breakdown": (
        "27930699e02a937e4f56cf5f90202adf6770cbe8a58391ced3e6801ee39b5339",
        "ed9fad4c3287f8a71c47b01344d1099862578de98289e5cecaf9b890a1c8bcfa",
    ),
    "claim-synthesis": (
        "03074fa07e82add33c1fb2681c81ba9b6cccb2bdb3c999d7f93f415dc554830b",
        "328257c271e2a32b791e2c50a43fdb213e79a1252a99e8ecd7351eedb2c381b4",
    ),
}
EXACT_RUBRIC_IDS = {
    "exact_evidence",
    "atomicity",
    "taxonomy_fit",
    "observation_interpretation_split",
    "applicability_limits",
    "provenance_complete",
    "accepted_current_atoms_only",
    "claim_input_order",
    "counterevidence",
    "rereview_non_mutating",
}
EXACT_INTERPRETER_MAPPINGS = tuple(
    (
        "book-analysis-taxonomy/v1",
        "com.plotpilot.novelagent.donor-analysis",
        capability,
        direction,
    )
    for capability in CAPABILITY_ORDER
    for direction in ("format_to_capability", "capability_to_format")
)


def strict_json(path: Path):
    def reject_duplicate(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = child
        return value

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def package_files(package_root: Path, expected: dict) -> dict[str, bytes]:
    files = {
        relative: (package_root / relative).read_bytes()
        for relative in expected["package_files"]
    }
    assert all((package_root / relative).is_file() for relative in files)
    assert (package_root / "files.sha256").read_text(encoding="utf-8") == expected["files_sha256"]
    return files


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def copy_identity_repository(destination: Path) -> tuple[Path, Path, Path]:
    repository = destination / "repository"
    data_root = repository / "data" / "book-analysis-taxonomy"
    skills_root = repository / "skills" / "donor-analysis"
    shutil.copytree(ROOT / "data" / "book-analysis-taxonomy", data_root)
    shutil.copytree(SKILLS_ROOT, skills_root)
    shutil.copytree(SDK_ROOT, repository / "sdk")
    shutil.copytree(ROOT / "contracts", repository / "contracts")
    return repository, data_root, skills_root


def verify_data_copy(root: Path) -> tuple[str, str]:
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / "identity.json")
    manifest = strict_json(root / "plugin.json")
    files = package_files(root, expected)
    verify_package_identity(
        files,
        manifest["plugin_id"],
        manifest["version"],
        identity["package_hash"],
        identity["release_id"],
        expected_files_sha256=(root / "files.sha256").read_bytes(),
    )
    if identity["package_hash"] != expected["package_hash"]:
        raise ValueError("data outer package_hash does not match expected identity")
    if identity["release_id"] != expected["release_id"]:
        raise ValueError("data outer release_id does not match expected identity")
    return identity["package_hash"], identity["release_id"]


def verify_skill_copy(root: Path) -> tuple[str, str]:
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / "identity.json")
    manifest = strict_json(root / "skill.json")
    files = package_files(root, expected)
    verify_skill_identity(
        files,
        manifest["skill_id"],
        manifest["version"],
        identity["skill_package_hash"],
        identity["skill_release_id"],
        expected_files_sha256=(root / "files.sha256").read_bytes(),
    )
    if identity["skill_package_hash"] != expected["skill_package_hash"]:
        raise ValueError("Skill outer package hash does not match expected identity")
    if identity["skill_release_id"] != expected["skill_release_id"]:
        raise ValueError("Skill outer release id does not match expected identity")
    return identity["skill_package_hash"], identity["skill_release_id"]


def assert_recursively_closed(schema, path="$") -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, path
        for key, value in schema.items():
            assert_recursively_closed(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            assert_recursively_closed(value, f"{path}[{index}]")


def test_data_package_identity_bundle_and_closed_manifest() -> None:
    manifest = strict_json(DATA_ROOT / "plugin.json")
    expected = strict_json(DATA_ROOT / "expected.json")
    identity = strict_json(DATA_ROOT / expected["identity_path"])
    assert manifest["kind"] == "data"
    assert manifest["data"] == {
        "format": "book-analysis-taxonomy/v1",
        "root": "data/taxonomy.json",
    }
    verify_manifest(manifest)
    files = package_files(DATA_ROOT, expected)
    verify_package_identity(
        files,
        manifest["plugin_id"],
        manifest["version"],
        expected["package_hash"],
        expected["release_id"],
        expected_files_sha256=(DATA_ROOT / "files.sha256").read_bytes(),
    )
    assert identity == {
        "schema": "book-analysis-taxonomy-package-identity/v1",
        "package_kind": "data",
        "plugin_id": manifest["plugin_id"],
        "format_id": "book-analysis-taxonomy/v1",
        "version": manifest["version"],
        "package_hash": expected["package_hash"],
        "release_id": expected["release_id"],
        "separate_from_code_plugin_id": "com.plotpilot.novelagent.donor-analysis",
    }
    bundle = strict_json(DATA_ROOT / "fixtures" / "plugin-data-bundle.json")
    verify_data_bundle(bundle)
    assert bundle["data_plugin_id"] == manifest["plugin_id"]
    assert bundle["data_release_id"] == expected["release_id"]
    assert bundle["package_hash"] == expected["package_hash"]
    assert bundle["bundle_hash"] == expected["bundle_hash"]


def test_taxonomy_covers_all_frozen_description_narration_language_structure_metrics() -> None:
    taxonomy = strict_json(DATA_ROOT / "data" / "taxonomy.json")
    assert taxonomy["schema"] == taxonomy["format_id"] == "book-analysis-taxonomy/v1"
    assert {item["key"] for item in taxonomy["description_types"]} == {
        "scenery", "appearance", "action", "combat", "psychology", "environment", "object"
    }
    assert {item["key"] for item in taxonomy["narration_techniques"]} == {
        "line_drawing", "fine_detail", "stream_of_consciousness", "symbolism", "metaphor", "contrast"
    }
    assert {item["key"] for item in taxonomy["ratio_metrics"]} == {
        "action_ratio", "psychology_ratio", "scenery_ratio"
    }
    assert {item["key"] for item in taxonomy["rhythm_emotion_metrics"]} == {
        "chapter_rhythm", "emotion_curve", "climax_buffer_sequence"
    }
    assert {item["key"] for item in taxonomy["language_metrics"]} == {
        "lexicon_and_idiom", "viewpoint", "grammatical_person", "tense"
    }
    assert {item["key"] for item in taxonomy["structure_metrics"]} == {
        "chapter_ending", "sentence_length", "paragraph_length", "segmentation", "punctuation_habits"
    }
    rubric_ids = {item["criterion_id"] for item in taxonomy["review_rubric"]}
    assert rubric_ids == EXACT_RUBRIC_IDS


def test_all_four_exact_interpreters_are_bidirectional_without_aliases() -> None:
    taxonomy = strict_json(DATA_ROOT / "data" / "taxonomy.json")
    mappings = taxonomy["interpreter_mappings"]
    observed = tuple(
        (
            item["format_id"],
            item["plugin_id"],
            item["capability_id"],
            item["direction"],
        )
        for item in mappings
    )
    assert observed == EXACT_INTERPRETER_MAPPINGS
    fixture = strict_json(DATA_ROOT / "fixtures" / "interpreter-roundtrip.json")
    assert tuple(fixture["capabilities"]) == CAPABILITY_ORDER
    assert fixture["forward_direction"] == "format_to_capability"
    assert fixture["reverse_direction"] == "capability_to_format"


@pytest.mark.parametrize(
    ("instance_name", "schema_name"),
    [
        ("data/taxonomy.json", "schemas/book-analysis-taxonomy-v1.schema.json"),
        ("fixtures/interpreter-roundtrip.json", "schemas/interpreter-roundtrip-v1.schema.json"),
    ],
)
def test_data_schemas_are_closed_and_reject_unknown_fields(instance_name: str, schema_name: str) -> None:
    schema = strict_json(DATA_ROOT / schema_name)
    Draft202012Validator.check_schema(schema)
    assert_recursively_closed(schema)
    value = strict_json(DATA_ROOT / instance_name)
    Draft202012Validator(schema).validate(value)
    tampered = deepcopy(value)
    tampered["unknown_field"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


@pytest.mark.parametrize(
    ("directory", "skill_id", "capabilities"),
    [
        (
            "atomic-breakdown",
            "com.plotpilot.skill.donor.atomic-breakdown",
            {"analysis.book.atom.extract/v1", "analysis.book.atom.manual/v1"},
        ),
        (
            "claim-synthesis",
            "com.plotpilot.skill.donor.claim-synthesis",
            {"analysis.book.claim.generate/v1"},
        ),
    ],
)
def test_skill_packages_are_closed_separately_identified_and_candidate_only(
    directory: str, skill_id: str, capabilities: set[str]
) -> None:
    root = SKILLS_ROOT / directory
    manifest = strict_json(root / "skill.json")
    method = strict_json(root / "method.json")
    schema = strict_json(root / "method.schema.json")
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / expected["identity_path"])
    assert_valid("plotpilot-skill/v1", manifest)
    assert manifest["skill_id"] == method["skill_id"] == skill_id
    assert method["input_format_id"] == "book-analysis-taxonomy/v1"
    assert set(method["capability_ids"]) == capabilities
    assert method["result_contract"] == "candidate-batch/v1"
    assert method["authority"] == "candidate_only"
    Draft202012Validator.check_schema(schema)
    assert_recursively_closed(schema)
    Draft202012Validator(schema).validate(method)
    tampered = deepcopy(method)
    tampered["unknown_field"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)
    files = package_files(root, expected)
    verify_skill_identity(
        files,
        skill_id,
        manifest["version"],
        expected["skill_package_hash"],
        expected["skill_release_id"],
        expected_files_sha256=(root / "files.sha256").read_bytes(),
    )
    assert identity == {
        "schema": "donor-skill-package-identity/v1",
        "package_kind": "skill",
        "skill_id": skill_id,
        "version": manifest["version"],
        "skill_package_hash": expected["skill_package_hash"],
        "skill_release_id": expected["skill_release_id"],
        "separate_from_code_plugin_id": "com.plotpilot.novelagent.donor-analysis",
        "separate_from_data_plugin_id": "com.plotpilot.novelagent.book-analysis-taxonomy",
    }


def test_code_data_and_two_skill_identities_are_distinct_and_frozen() -> None:
    code = strict_json(ROOT / "plugins" / "donor-analysis" / "expected.json")
    data_expected = strict_json(DATA_ROOT / "expected.json")
    atomic = strict_json(SKILLS_ROOT / "atomic-breakdown" / "expected.json")
    claim = strict_json(SKILLS_ROOT / "claim-synthesis" / "expected.json")
    hashes = {
        code["package_hash"],
        data_expected["package_hash"],
        atomic["skill_package_hash"],
        claim["skill_package_hash"],
    }
    releases = {
        code["release_id"],
        data_expected["release_id"],
        atomic["skill_release_id"],
        claim["skill_release_id"],
    }
    assert len(hashes) == len(releases) == 4
    assert (data_expected["package_hash"], data_expected["release_id"]) == FROZEN_IDENTITIES["data"]
    assert (atomic["skill_package_hash"], atomic["skill_release_id"]) == FROZEN_IDENTITIES["atomic-breakdown"]
    assert (claim["skill_package_hash"], claim["skill_release_id"]) == FROZEN_IDENTITIES["claim-synthesis"]
    assert data_expected["identity_domains"]["data_or_code"] == "plotpilot-release/v1"
    assert atomic["identity_domains"]["skill"] == claim["identity_domains"]["skill"] == "plotpilot-skill-release/v1"


def test_data_and_skill_unchanged_double_rebuild_is_byte_stable(tmp_path: Path) -> None:
    repository, data_package, skills_root = copy_identity_repository(tmp_path)
    baseline_data = snapshot(data_package)
    baseline_skills = snapshot(skills_root)
    commands = (
        [sys.executable, str(data_package / "rebuild_package.py")],
        [sys.executable, str(skills_root / "rebuild_packages.py")],
    )
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
        assert snapshot(data_package) == baseline_data
        assert snapshot(skills_root) == baseline_skills
    assert verify_data_copy(data_package / "v1") == FROZEN_IDENTITIES["data"]
    assert verify_skill_copy(skills_root / "atomic-breakdown") == FROZEN_IDENTITIES["atomic-breakdown"]
    assert verify_skill_copy(skills_root / "claim-synthesis") == FROZEN_IDENTITIES["claim-synthesis"]


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("data", "data/taxonomy.json"),
        ("atomic-breakdown", "prompt.txt"),
        ("claim-synthesis", "method.json"),
    ],
)
def test_data_or_skill_semantic_change_rejects_frozen_identity_and_changes_digest(
    tmp_path: Path, kind: str, relative: str
) -> None:
    _, data_package, skills_root = copy_identity_repository(tmp_path)
    root = data_package / "v1" if kind == "data" else skills_root / kind
    expected = strict_json(root / "expected.json")
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\nsemantic-drift\n")
    files = package_files(root, expected)
    frozen_hash, frozen_release = FROZEN_IDENTITIES[kind]
    if kind == "data":
        manifest = strict_json(root / "plugin.json")
        changed = digest_package(files, manifest["plugin_id"], manifest["version"])
        changed_pair = (changed.package_hash, changed.release_id)
        with pytest.raises((RuntimeError, ValueError, OSError)):
            verify_package_identity(
                files,
                manifest["plugin_id"],
                manifest["version"],
                frozen_hash,
                frozen_release,
                expected_files_sha256=(root / "files.sha256").read_bytes(),
            )
    else:
        manifest = strict_json(root / "skill.json")
        changed_hash = skill_package_hash(files)
        changed_pair = (
            changed_hash,
            skill_release_id(manifest["skill_id"], manifest["version"], changed_hash),
        )
        with pytest.raises((RuntimeError, ValueError, OSError)):
            verify_skill_identity(
                files,
                manifest["skill_id"],
                manifest["version"],
                frozen_hash,
                frozen_release,
                expected_files_sha256=(root / "files.sha256").read_bytes(),
            )
    assert changed_pair != (frozen_hash, frozen_release)


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("data", "files.sha256"),
        ("data", "expected.json"),
        ("data", "identity.json"),
        ("atomic-breakdown", "files.sha256"),
        ("atomic-breakdown", "expected.json"),
        ("atomic-breakdown", "identity.json"),
        ("claim-synthesis", "files.sha256"),
        ("claim-synthesis", "expected.json"),
        ("claim-synthesis", "identity.json"),
    ],
)
def test_data_and_skill_missing_manifests_or_identity_fail_closed(
    tmp_path: Path, kind: str, relative: str
) -> None:
    _, data_package, skills_root = copy_identity_repository(tmp_path)
    root = data_package / "v1" if kind == "data" else skills_root / kind
    (root / relative).unlink()
    verifier = verify_data_copy if kind == "data" else verify_skill_copy
    with pytest.raises((RuntimeError, ValueError, OSError)):
        verifier(root)


@pytest.mark.parametrize("kind", ["data", "atomic-breakdown", "claim-synthesis"])
def test_data_and_skill_outer_identity_mismatch_fails_closed(tmp_path: Path, kind: str) -> None:
    _, data_package, skills_root = copy_identity_repository(tmp_path)
    root = data_package / "v1" if kind == "data" else skills_root / kind
    identity_path = root / "identity.json"
    identity = strict_json(identity_path)
    field = "package_hash" if kind == "data" else "skill_package_hash"
    identity[field] = "f" * 64
    identity_path.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8", newline="\n")
    verifier = verify_data_copy if kind == "data" else verify_skill_copy
    with pytest.raises((RuntimeError, ValueError, OSError)):
        verifier(root)
