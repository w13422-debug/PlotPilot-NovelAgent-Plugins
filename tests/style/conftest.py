from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
MANUFACTURING_ROOT = ROOT / "plugins" / "style-manufacturing"
RUNTIME_ROOT = ROOT / "plugins" / "style-runtime"
STYLE_DATA_ROOT = ROOT / "data" / "style" / "v1"
LEXICON_DATA_ROOT = ROOT / "data" / "lexicon" / "v1"
SKILLS_ROOT = ROOT / "skills" / "style"

for path in (SDK_ROOT, MANUFACTURING_ROOT, RUNTIME_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


HEX64 = re.compile(r"^[0-9a-f]{64}$")


def strict_json(path: Path) -> Any:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"UTF-8 BOM is forbidden: {path}")
    return json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def assert_recursively_closed(schema: Any, location: str = "$") -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, location
        for key, value in schema.items():
            assert_recursively_closed(value, f"{location}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            assert_recursively_closed(value, f"{location}[{index}]")


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def parse_files_manifest(root: Path) -> tuple[bytes, dict[str, bytes]]:
    raw = (root / "files.sha256").read_bytes()
    assert raw and not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    files: dict[str, bytes] = {}
    names: list[str] = []
    for line in raw.decode("utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        assert separator == "  " and HEX64.fullmatch(digest), line
        assert relative and "\\" not in relative and relative != "files.sha256"
        assert relative not in names, relative
        path = root / relative
        assert path.is_file(), relative
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest, relative
        names.append(relative)
        files[relative] = content
    assert names == sorted(names, key=lambda value: value.encode("utf-8"))
    return raw, files


def independently_recompute_code_or_data_identity(root: Path) -> tuple[str, str]:
    manifest = strict_json(root / "plugin.json")
    raw, _ = parse_files_manifest(root)
    package_hash = hashlib.sha256(b"plotpilot-package/v1\n" + raw).hexdigest()
    release_payload = (
        f"plotpilot-release/v1\n{manifest['plugin_id']}\n"
        f"{manifest['version']}\n{package_hash}\n"
    ).encode("ascii")
    return package_hash, hashlib.sha256(release_payload).hexdigest()


def independently_recompute_skill_identity(root: Path) -> tuple[str, str]:
    manifest = strict_json(root / "skill.json")
    raw, _ = parse_files_manifest(root)
    package_hash = hashlib.sha256(b"plotpilot-skill-package/v1\n" + raw).hexdigest()
    release_payload = (
        f"plotpilot-skill-release/v1\n{manifest['skill_id']}\n"
        f"{manifest['version']}\n{package_hash}\n"
    ).encode("ascii")
    return package_hash, hashlib.sha256(release_payload).hexdigest()


def verify_code_or_data_evidence(root: Path) -> tuple[str, str]:
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / expected["identity_path"])
    actual = independently_recompute_code_or_data_identity(root)
    if actual != (expected["package_hash"], expected["release_id"]):
        raise ValueError("outer expected code/data identity does not match payload")
    if actual != (identity["package_hash"], identity["release_id"]):
        raise ValueError("outer code/data identity sidecar does not match payload")
    return actual


def verify_skill_evidence(root: Path) -> tuple[str, str]:
    expected = strict_json(root / "expected.json")
    identity = strict_json(root / expected["identity_path"])
    actual = independently_recompute_skill_identity(root)
    if actual != (expected["skill_package_hash"], expected["skill_release_id"]):
        raise ValueError("outer expected Skill identity does not match payload")
    if actual != (identity["skill_package_hash"], identity["skill_release_id"]):
        raise ValueError("outer Skill identity sidecar does not match payload")
    return actual


def copy_style_repository(destination: Path) -> Path:
    repository = destination / "repository"
    for relative in (
        "contracts",
        "sdk",
        "plugins/style-manufacturing",
        "plugins/style-runtime",
        "data/style",
        "data/lexicon",
        "skills/style",
    ):
        source = ROOT / relative
        if source.exists():
            shutil.copytree(source, repository / relative)
    return repository


def run_python(script: Path, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.fixture(autouse=True)
def forbid_repository_cache_creation() -> None:
    before = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    }
    yield
    after = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    }
    assert after == before, f"tests created repository caches: {sorted(after - before)}"
