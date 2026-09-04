from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from asset_derivation.package_identity import calculate_identity as calculate_asset_identity
from character_distillation.package_identity import calculate_identity as calculate_character_identity
from plotpilot_plugin_sdk.package import digest_package, skill_package_hash, skill_release_id


ROOT = Path(__file__).resolve().parents[2]


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "script",
    [
        ROOT / "plugins" / "character-distillation" / "rebuild_package.py",
        ROOT / "plugins" / "asset-derivation" / "rebuild_package.py",
        ROOT / "data" / "character" / "character-archetype" / "rebuild_package.py",
        ROOT / "data" / "world" / "world-rule-template" / "rebuild_package.py",
        ROOT / "skills" / "character" / "rebuild_packages.py",
    ],
)
def test_two_rebuilds_are_byte_identical(script: Path) -> None:
    """Every immutable package builder must be stable across two rebuilds."""
    first = subprocess.run(
        [sys.executable, script.name, "--rebuild"],
        cwd=script.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [sys.executable, script.name, "--rebuild"],
        cwd=script.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    assert first.stdout == second.stdout


def test_code_identity_fresh_copy_and_tamper_rejection(tmp_path: Path) -> None:
    for name, verifier in (
        ("character-distillation", calculate_character_identity),
        ("asset-derivation", calculate_asset_identity),
    ):
        source = ROOT / "plugins" / name
        copy_root = tmp_path / name
        shutil.copytree(source, copy_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        assert verifier(copy_root).package_hash == _json(source / "expected.json")["package_hash"]
        tampered = copy_root / ("character_distillation" if "character" in name else "asset_derivation") / "runtime.py"
        tampered.write_bytes(tampered.read_bytes() + b"\n# tamper\n")
        with pytest.raises(RuntimeError):
            verifier(copy_root)


def test_data_and_skill_sidecars_recompute_with_sdk() -> None:
    for relative in (
        Path("data/character/character-archetype/v1"),
        Path("data/world/world-rule-template/v1"),
    ):
        package_root = ROOT / relative
        expected = _json(package_root / "expected.json")
        files = {
            path: (package_root / path).read_bytes()
            for path in expected["package_files"]
        }
        digest = digest_package(files, str(expected["plugin_id"]), str(expected["version"]))
        assert digest.package_hash == expected["package_hash"]
        assert digest.release_id == expected["release_id"]

    skill_root = ROOT / "skills" / "character" / "character-line"
    expected = _json(skill_root / "expected.json")
    files = {path: (skill_root / path).read_bytes() for path in expected["package_files"]}
    digest = skill_package_hash(files)
    assert digest == expected["skill_package_hash"]
    assert skill_release_id(str(expected["skill_id"]), str(expected["version"]), digest) == expected["skill_release_id"]


def test_wheels_have_deterministic_zip_identity() -> None:
    for wheel in (
        ROOT / "plugins" / "character-distillation" / "backend" / "plotpilot_character_distillation-0.1.0-py3-none-any.whl",
        ROOT / "plugins" / "asset-derivation" / "backend" / "plotpilot_asset_derivation-0.1.0-py3-none-any.whl",
    ):
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert names == sorted(names, key=lambda value: value.encode("utf-8"))
            assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
            assert not any(name.endswith("identity.json") for name in names)
