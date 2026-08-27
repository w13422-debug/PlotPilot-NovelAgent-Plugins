from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    assert_valid,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_skill_identity,
    verify_skill_receipt,
)


DEMOS = ROOT / "catalog" / "demos"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _package_files(package_dir: Path, expected: dict[str, object]) -> dict[str, bytes]:
    paths = expected["package_files"]
    assert isinstance(paths, list)
    files = {}
    for relative in paths:
        assert isinstance(relative, str)
        path = package_dir / Path(relative)
        assert path.is_file(), path
        files[relative] = path.read_bytes()
    assert (package_dir / "files.sha256").read_bytes().decode("utf-8") == expected["files_sha256"]
    return files


def test_catalog_has_canonical_code_data_and_skill_demo_layout() -> None:
    assert sorted(path.name for path in DEMOS.iterdir() if path.is_dir()) == ["code", "data", "skill"]
    assert (DEMOS / "code" / "plugin.json").is_file()
    assert (DEMOS / "data" / "plugin.json").is_file()
    assert (DEMOS / "skill" / "skill.json").is_file()
    for package_dir in (DEMOS / "code", DEMOS / "data", DEMOS / "skill"):
        assert (package_dir / "files.sha256").is_file()
        assert (package_dir / "expected.json").is_file()


def test_code_demo_package_receipt_and_backend_type_gate_fixture() -> None:
    package_dir = DEMOS / "code"
    manifest = _json(package_dir / "plugin.json")
    expected = _json(package_dir / "expected.json")
    assert manifest["schema"] == "plotpilot-plugin/v1"
    assert manifest["kind"] == "code"
    verify_manifest(manifest)
    files = _package_files(package_dir, expected)
    verify_package_identity(
        files,
        manifest["plugin_id"],
        manifest["version"],
        expected["package_hash"],
        expected["release_id"],
        expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
    )
    backend = manifest["backend"]
    storage = manifest["storage"]
    assert (package_dir / backend["wheel"]).is_file()
    assert (package_dir / backend["requirements_lock"]).is_file()
    assert (package_dir / backend["wheelhouse"]).is_dir()
    assert (package_dir / storage["migration_manifest"]).is_file()
    assert_valid("settings-migration-manifest/v1", _json(package_dir / "migrations" / "manifest.json"))

    result = _json(package_dir / "fixtures" / "result-bundle.json")
    receipt = _json(package_dir / "fixtures" / "provenance-receipt.json")
    verify_result_bundle(result)
    verify_provenance_receipt(receipt)
    assert result["provenance_receipt_id"] == receipt["receipt_id"] == expected["receipt_id"]
    assert receipt["receipt_hash"] == expected["receipt_hash"]
    assert receipt["package_hash"] == expected["package_hash"]
    assert receipt["release_id"] == expected["release_id"]


def test_data_demo_package_and_data_bundle_fixture() -> None:
    package_dir = DEMOS / "data"
    manifest = _json(package_dir / "plugin.json")
    expected = _json(package_dir / "expected.json")
    assert manifest["schema"] == "plotpilot-plugin/v1"
    assert manifest["kind"] == "data"
    verify_manifest(manifest)
    files = _package_files(package_dir, expected)
    verify_package_identity(
        files,
        manifest["plugin_id"],
        manifest["version"],
        expected["package_hash"],
        expected["release_id"],
        expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
    )
    bundle = _json(package_dir / "fixtures" / "plugin-data-bundle.json")
    verify_data_bundle(bundle)
    assert bundle["data_plugin_id"] == manifest["plugin_id"]
    assert bundle["data_release_id"] == expected["release_id"]
    assert bundle["package_hash"] == expected["package_hash"]
    assert bundle["bundle_hash"] == expected["bundle_hash"]


def test_skill_demo_package_and_frozen_skill_receipt_fixture() -> None:
    package_dir = DEMOS / "skill"
    manifest = _json(package_dir / "skill.json")
    expected = _json(package_dir / "expected.json")
    assert_valid("plotpilot-skill/v1", manifest)
    assert manifest["schema"] == "plotpilot-skill/v1"
    files = _package_files(package_dir, expected)
    verify_skill_identity(
        files,
        manifest["skill_id"],
        manifest["version"],
        expected["skill_package_hash"],
        expected["skill_release_id"],
        expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
    )
    receipt = _json(package_dir / "fixtures" / "skill-run-receipt.json")
    verify_skill_receipt(receipt)
    assert receipt["skill_id"] == manifest["skill_id"]
    assert receipt["package_hash"] == expected["skill_package_hash"]
    assert receipt["release_id"] == expected["skill_release_id"]
    assert receipt["receipt_hash"] == expected["receipt_hash"]


def test_demo_hash_vectors_are_deterministic_not_only_self_consistent() -> None:
    assert _json(DEMOS / "code" / "expected.json")["package_hash"] == "9577ff6df8742c8f54b922e3ccdb2348f250336ca4246ca58153201aa4fb5590"
    assert _json(DEMOS / "data" / "expected.json")["package_hash"] == "28f55a1a08e23bfd650ffab1c1c072559ef8237300566cba1ee00f03a4edfe6d"
    assert _json(DEMOS / "skill" / "expected.json")["skill_package_hash"] == "69574c04055f92f3e94136b0ecbf3abe1e16f719e61c81a9a4af7e798f56394a"
