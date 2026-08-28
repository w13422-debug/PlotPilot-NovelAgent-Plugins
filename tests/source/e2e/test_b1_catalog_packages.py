from __future__ import annotations

import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_capability_descriptor,
    verify_manifest,
    verify_package_identity,
)


PLUGIN_DIRS = {
    "com.plotpilot.novelagent.source-import": ROOT / "plugins" / "source-import",
    "com.plotpilot.novelagent.source-cleaning-runtime": ROOT / "plugins" / "source-cleaning-runtime",
    "com.plotpilot.novelagent.source-structure": ROOT / "plugins" / "source-structure",
}
MODULES = {
    "com.plotpilot.novelagent.source-import": "source_import",
    "com.plotpilot.novelagent.source-cleaning-runtime": "source_cleaning_runtime",
    "com.plotpilot.novelagent.source-structure": "source_structure",
}


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_entries() -> dict[str, dict[str, object]]:
    catalog = _json(ROOT / "catalog" / "plugin-catalog-v1.json")
    entries = {
        entry["plugin_id"]: entry
        for entry in catalog["code_plugins"]
        if entry["plugin_id"] in PLUGIN_DIRS
    }
    assert set(entries) == set(PLUGIN_DIRS)
    return entries


def _payload_files(plugin_dir: Path) -> tuple[dict[str, bytes], bytes]:
    raw = (plugin_dir / "files.sha256").read_bytes()
    text = raw.decode("utf-8", errors="strict")
    assert raw and not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    files: dict[str, bytes] = {}
    for line in text.splitlines():
        digest, separator, relative = line.partition("  ")
        assert separator == "  " and len(digest) == 64 and relative
        assert relative not in files and relative != "files.sha256"
        data = (plugin_dir / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        files[relative] = data
    assert files
    return files, raw


def _descriptor_digest(expected: dict[str, object], relative: str) -> str:
    """Resolve both the legacy per-file fields and the normalized digest map."""
    normalized = expected.get("descriptor_sha256")
    if isinstance(normalized, dict):
        digest = normalized.get(relative)
    elif relative == "descriptor.json":
        digest = normalized
    else:
        suffix = Path(relative).stem.removeprefix("descriptor-").replace("-", "_")
        digest = expected.get(f"descriptor_{suffix}_sha256")
    assert isinstance(digest, str) and len(digest) == 64, (relative, digest)
    return digest


def _closed_package_paths(plugin_dir: Path) -> list[str]:
    controller_metadata = {".gitattributes", "expected.json"}
    return sorted(
        path.relative_to(plugin_dir).as_posix()
        for path in plugin_dir.rglob("*")
        if path.is_file()
        and path.relative_to(plugin_dir).as_posix() not in controller_metadata
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )


def test_b1_manifests_descriptors_and_schema_indexes_match_frozen_catalog() -> None:
    catalog = _catalog_entries()
    for plugin_id, plugin_dir in PLUGIN_DIRS.items():
        manifest = _json(plugin_dir / "plugin.json")
        expected = _json(plugin_dir / "expected.json")
        assert expected["package_files"] == sorted(expected["package_files"], key=lambda value: value.encode("utf-8"))
        assert _closed_package_paths(plugin_dir) == expected["package_files"]
        verify_manifest(manifest)
        assert manifest["plugin_id"] == plugin_id
        assert manifest["kind"] == "code"
        assert manifest["needs"] == catalog[plugin_id]["needs"]
        assert manifest["backend"]["wheel"] == expected["wheel_path"]
        assert manifest["backend"]["entrypoint"].split(":", 1)[0].split(".", 1)[0] == MODULES[plugin_id]

        frozen_caps = {row["capability_id"]: row for row in catalog[plugin_id]["capabilities"]}
        manifest_caps = {row["capability_id"]: row for row in manifest["capabilities"]}
        assert set(manifest_caps) == set(frozen_caps)
        assert set(expected["schema_index_paths"]) == set(frozen_caps)

        descriptors = {}
        for relative in expected["descriptor_paths"]:
            path = plugin_dir / relative
            descriptor = _json(path)
            cap = descriptor["capability_id"]
            assert cap not in descriptors
            frozen = frozen_caps[cap]
            verify_capability_descriptor(
                descriptor,
                expected_capability_id=cap,
                expected_provider={"plugin_id": plugin_id, "release_id": expected["release_id"]},
            )
            assert descriptor["input_schema"] == frozen["input_schema"]
            assert descriptor["output_schema"] == frozen["output_schema"]
            assert descriptor["result_contract"] == frozen["result_contract"]
            assert descriptor["supports"] == frozen["operations"]
            assert descriptor["deterministic"] is frozen["deterministic"]
            assert descriptor["accepted_data_formats"] == frozen["accepted_data_formats"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == _descriptor_digest(expected, relative)
            descriptors[cap] = descriptor
        assert set(descriptors) == set(frozen_caps)

        module_root = plugin_dir / MODULES[plugin_id]
        for cap, relative in expected["schema_index_paths"].items():
            index_path = plugin_dir / relative
            index = _json(index_path)
            assert set(index) == {"schema", "plugin_id", "capability_id", "schemas"}
            assert index["schema"] == "provider-schema-index/v1"
            assert index["plugin_id"] == plugin_id
            assert index["capability_id"] == cap
            ids = {row["schema_id"] for row in index["schemas"]}
            assert ids == {frozen_caps[cap]["input_schema"], frozen_caps[cap]["output_schema"]}
            for row in index["schemas"]:
                relative_schema = Path(row["path"])
                assert not relative_schema.is_absolute()
                assert relative_schema.parts[0] == "schemas"
                data = (module_root / relative_schema).read_bytes()
                assert hashlib.sha256(data).hexdigest() == row["sha256"]


def test_b1_outer_package_identities_are_exact_and_wheels_are_not_self_circular() -> None:
    for plugin_id, plugin_dir in PLUGIN_DIRS.items():
        expected = _json(plugin_dir / "expected.json")
        files, manifest_raw = _payload_files(plugin_dir)
        assert manifest_raw.decode("utf-8") == expected["files_sha256"]
        verify_package_identity(
            files,
            plugin_id,
            expected["version"],
            expected["package_hash"],
            expected["release_id"],
            expected_files_sha256=manifest_raw,
        )
        identity_path = plugin_dir / expected.get("identity_path", f"{MODULES[plugin_id]}/identity.json")
        identity = _json(identity_path)
        assert identity["plugin_id"] == plugin_id
        assert identity["version"] == expected["version"]
        assert identity["package_hash"] == expected["package_hash"]
        assert identity["release_id"] == expected["release_id"]
        wheel = plugin_dir / expected["wheel_path"]
        with zipfile.ZipFile(wheel) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
            assert f"{MODULES[plugin_id]}/identity.json" not in names
            assert f"{MODULES[plugin_id]}/package_identity.py" in names


def test_b1_code_plugins_do_not_import_each_other_or_declare_remote_dependencies() -> None:
    private_modules = set(MODULES.values())
    for plugin_id, plugin_dir in PLUGIN_DIRS.items():
        own = MODULES[plugin_id]
        for path in (plugin_dir / own).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = {node.module.split(".", 1)[0]}
                else:
                    continue
                assert not (roots & (private_modules - {own})), (path, roots)
        lock = (plugin_dir / "backend" / "requirements.lock").read_text(encoding="utf-8")
        lowered = lock.casefold()
        assert "http://" not in lowered and "https://" not in lowered
        assert "git+" not in lowered and "--index-url" not in lowered
