from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import (
    LEXICON_DATA_ROOT,
    MANUFACTURING_ROOT,
    RUNTIME_ROOT,
    SKILLS_ROOT,
    STYLE_DATA_ROOT,
    assert_recursively_closed,
    copy_style_repository,
    independently_recompute_code_or_data_identity,
    independently_recompute_skill_identity,
    run_python,
    snapshot,
    strict_json,
    verify_code_or_data_evidence,
    verify_skill_evidence,
)
from jsonschema import Draft202012Validator, ValidationError
from style_manufacturing.contract import (
    exact_release_eligible,
    merge_lexicons,
    validate_lexicon,
    validate_qualification_receipt,
    validate_style_pack,
)

CODE_ROOTS = (MANUFACTURING_ROOT, RUNTIME_ROOT)
DATA_ROOTS = (STYLE_DATA_ROOT, LEXICON_DATA_ROOT)
SKILL_IDS = {
    "donor-style-analysis": "com.plotpilot.skill.donor.style-analysis",
    "style-apply": "com.plotpilot.skill.style.apply",
}


def test_code_package_identities_are_independently_recomputed_and_tamper_fail_closed(tmp_path: Path) -> None:
    for root in CODE_ROOTS:
        expected = strict_json(root / "expected.json")
        identity = strict_json(root / expected["identity_path"])
        package_hash, release_id = independently_recompute_code_or_data_identity(root)
        assert (package_hash, release_id) == (expected["package_hash"], expected["release_id"])
        assert identity["package_hash"] == package_hash
        assert identity["release_id"] == release_id

        copied = tmp_path / root.name
        shutil.copytree(root, copied)
        manifest_line = (copied / "files.sha256").read_text(encoding="utf-8").splitlines()[0]
        relative = manifest_line.split("  ", 1)[1]
        (copied / relative).write_bytes((copied / relative).read_bytes() + b"\ntamper\n")
        with pytest.raises(AssertionError):
            independently_recompute_code_or_data_identity(copied)


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("style-data", "data/style/v1/data/style-pack.json"),
        ("lexicon-data", "data/lexicon/v1/data/lexicon.json"),
        ("analysis-skill", "skills/style/donor-style-analysis/prompt.txt"),
        ("apply-skill", "skills/style/style-apply/method.json"),
    ],
)
def test_data_and_skill_semantic_tamper_changes_or_invalidates_frozen_identity(
    tmp_path: Path, kind: str, relative: str
) -> None:
    repository = copy_style_repository(tmp_path)
    target = repository / relative
    target.write_bytes(target.read_bytes() + b"\nsemantic-tamper\n")
    root = target.parents[1] if kind.endswith("data") else target.parent
    verifier = verify_code_or_data_evidence if kind.endswith("data") else verify_skill_evidence
    with pytest.raises((AssertionError, KeyError, ValueError)):
        verifier(root)


def test_data_packages_have_exact_formats_closed_schemas_and_independent_identities() -> None:
    expected_formats = {STYLE_DATA_ROOT: "style-pack/v1", LEXICON_DATA_ROOT: "lexicon/v1"}
    for root, format_id in expected_formats.items():
        manifest = strict_json(root / "plugin.json")
        expected = strict_json(root / "expected.json")
        identity = strict_json(root / expected["identity_path"])
        assert manifest["kind"] == "data"
        assert manifest["data"]["format"] == format_id
        assert identity["package_kind"] == "data"
        assert identity["format_id"] == format_id
        package_hash, release_id = independently_recompute_code_or_data_identity(root)
        assert (package_hash, release_id) == (expected["package_hash"], expected["release_id"])
        for path in root.rglob("*.schema.json"):
            schema = strict_json(path)
            Draft202012Validator.check_schema(schema)
            assert_recursively_closed(schema, path.relative_to(root).as_posix())


def test_style_data_fixture_is_an_exact_qualified_release_not_a_skill_or_code_identity() -> None:
    fixture = strict_json(STYLE_DATA_ROOT / "fixtures" / "exact-qualified-release.json")
    pack = validate_style_pack(fixture["style_pack"])
    receipt = validate_qualification_receipt(
        fixture["qualification_receipt"], expected_style_release_id=pack["style_release_id"]
    )
    assert fixture["exact_release_eligible"] is True
    assert exact_release_eligible(pack, receipt) is True
    assert fixture["identity_domains"] == {
        "style": "style-release/v1",
        "data_or_code": "plotpilot-release/v1",
        "skill": "plotpilot-skill-release/v1",
        "code_plugin_id": "com.plotpilot.novelagent.style-manufacturing",
    }
    assert strict_json(STYLE_DATA_ROOT / "data" / "style-pack.json") == pack


def test_lexicon_data_fixture_recomputes_the_exact_deterministic_conflict_result() -> None:
    fixture = strict_json(LEXICON_DATA_ROOT / "fixtures" / "merge-conflicts.json")
    inputs = [validate_lexicon(value) for value in fixture["inputs"]]
    assert merge_lexicons(inputs) == validate_lexicon(fixture["expected"])
    assert set(fixture["conflict_rules"]) == {
        "higher priority wins",
        "equal priority uses source_plugin_id UTF-8 byte order",
        "exact tie uses canonical row bytes",
        "output uses normalized_term UTF-8 byte order",
    }
    validate_lexicon(strict_json(LEXICON_DATA_ROOT / "data" / "lexicon.json"))


def test_skills_are_closed_catalog_exact_and_have_independent_skill_hash_domains() -> None:
    for directory, skill_id in SKILL_IDS.items():
        root = SKILLS_ROOT / directory
        manifest = strict_json(root / "skill.json")
        method = strict_json(root / "method.json")
        schema = strict_json(root / "method.schema.json")
        expected = strict_json(root / "expected.json")
        identity = strict_json(root / expected["identity_path"])
        assert manifest["skill_id"] == method["skill_id"] == skill_id
        assert identity["package_kind"] == "skill"
        assert identity["skill_id"] == skill_id
        Draft202012Validator.check_schema(schema)
        assert_recursively_closed(schema)
        Draft202012Validator(schema).validate(method)
        invalid = deepcopy(method)
        invalid["unknown_field"] = True
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(invalid)
        package_hash, release_id = independently_recompute_skill_identity(root)
        assert (package_hash, release_id) == (
            expected["skill_package_hash"],
            expected["skill_release_id"],
        )


def test_code_data_and_skill_identities_are_permanently_separate() -> None:
    package_hashes: set[str] = set()
    release_ids: set[str] = set()
    owners: set[str] = set()
    for root in (*CODE_ROOTS, *DATA_ROOTS):
        manifest = strict_json(root / "plugin.json")
        expected = strict_json(root / "expected.json")
        package_hashes.add(expected["package_hash"])
        release_ids.add(expected["release_id"])
        owners.add(manifest["plugin_id"])
    for directory, skill_id in SKILL_IDS.items():
        expected = strict_json(SKILLS_ROOT / directory / "expected.json")
        package_hashes.add(expected["skill_package_hash"])
        release_ids.add(expected["skill_release_id"])
        owners.add(skill_id)
    assert len(package_hashes) == len(release_ids) == len(owners) == 6


def test_all_six_packages_are_byte_identical_across_two_isolated_rebuilds(tmp_path: Path) -> None:
    repository = copy_style_repository(tmp_path)
    tracked_roots = [
        repository / "plugins/style-manufacturing",
        repository / "plugins/style-runtime",
        repository / "data/style",
        repository / "data/lexicon",
        repository / "skills/style",
    ]
    scripts = [
        repository / "plugins/style-manufacturing/rebuild_package.py",
        repository / "plugins/style-runtime/rebuild_package.py",
        repository / "data/style/rebuild_package.py",
        repository / "data/lexicon/rebuild_package.py",
        repository / "skills/style/rebuild_packages.py",
    ]
    baseline = {root.as_posix(): snapshot(root) for root in tracked_roots}
    for _ in range(2):
        for script in scripts:
            completed = run_python(script, cwd=repository)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            assert completed.stdout.strip()
        assert {root.as_posix(): snapshot(root) for root in tracked_roots} == baseline


@pytest.mark.parametrize(
    "relative",
    [
        "plugins/style-manufacturing/files.sha256",
        "plugins/style-runtime/expected.json",
        "data/style/v1/identity.json",
        "data/lexicon/v1/files.sha256",
        "skills/style/donor-style-analysis/identity.json",
        "skills/style/style-apply/files.sha256",
    ],
)
def test_missing_identity_evidence_fails_closed(tmp_path: Path, relative: str) -> None:
    repository = copy_style_repository(tmp_path)
    target = repository / relative
    target.unlink()
    if relative.startswith(("plugins/", "data/")):
        root = target.parent
        with pytest.raises((AssertionError, FileNotFoundError, KeyError, ValueError)):
            verify_code_or_data_evidence(root)
    else:
        root = target.parent
        with pytest.raises((AssertionError, FileNotFoundError, KeyError, ValueError)):
            verify_skill_evidence(root)
