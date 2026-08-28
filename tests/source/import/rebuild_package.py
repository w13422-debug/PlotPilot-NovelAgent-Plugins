"""Rebuild source-import's deterministic PEP 427 wheel and identity sidecars."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

PLUGIN_ROOT = ROOT / "plugins" / "source-import"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from plotpilot_plugin_sdk.package import digest_package
from source_import.build_backend import build_canonical_wheel
MODULE = "source_import"
PLUGIN_ID = "com.plotpilot.novelagent.source-import"
VERSION = "0.1.0"
DIST = "plotpilot_source_import"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
GENERATED = {"descriptor.json", "descriptor-parse.json", "files.sha256", "expected.json", f"{MODULE}/identity.json"}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


def _canonical_files() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        name = path.relative_to(PLUGIN_ROOT).as_posix()
        if name in GENERATED or path.name == ".gitattributes":
            continue
        result[name] = path.read_bytes()
    return result


def _package_files() -> list[str]:
    result = []
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or path.name in {".gitattributes", "files.sha256", "expected.json"} or "__pycache__" in path.parts:
            continue
        result.append(path.relative_to(PLUGIN_ROOT).as_posix())
    result.append("files.sha256")
    return sorted(result, key=lambda name: name.encode("utf-8"))


def _schema_index(capability_id: str, schema_pairs: tuple[tuple[str, str], ...]) -> dict[str, object]:
    return {
        "schema": "provider-schema-index/v1",
        "plugin_id": PLUGIN_ID,
        "capability_id": capability_id,
        "schemas": [
            {
                "schema_id": schema_id,
                "path": path,
                "sha256": hashlib.sha256((PLUGIN_ROOT / MODULE / path).read_bytes()).hexdigest(),
            }
            for schema_id, path in schema_pairs
        ],
    }



def _update_schema_index() -> None:
    schema_dir = PLUGIN_ROOT / MODULE / "schemas"
    indexes = {
        "inspect-index.json": _schema_index(
            "source.import.inspect/v1",
            (
                ("source.import.inspect-request/v1", "schemas/inspect-input.schema.json"),
                ("source.import.inspect-result/v1", "schemas/inspect-output.schema.json"),
            ),
        ),
        "parse-index.json": _schema_index(
            "source.import.parse/v1",
            (
                ("source.import.parse-request/v1", "schemas/parse-input.schema.json"),
                ("source.import.parse-result/v1", "schemas/parse-output.schema.json"),
            ),
        ),
    }
    for name, index in indexes.items():
        _write_json(schema_dir / name, index)


def rebuild() -> dict[str, object]:
    _update_schema_index()
    # The wheel is canonical and excludes the outer install-scope identity sidecar.
    wheel = build_canonical_wheel(PLUGIN_ROOT, PLUGIN_ROOT / "backend")
    files = _canonical_files()
    digest = digest_package(files, PLUGIN_ID, VERSION)
    wheel_name = wheel.relative_to(PLUGIN_ROOT).as_posix()
    manifest_names = [line[:-1].split("  ", 1)[1] for line in digest.files_sha256.decode("utf-8").splitlines(True)]
    if set(manifest_names) != set(files) or wheel_name not in manifest_names:
        raise RuntimeError("source-import canonical payload does not match public SDK manifest")
    (PLUGIN_ROOT / "files.sha256").write_bytes(digest.files_sha256)
    identity = {
        "schema": "source-import-package-identity/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
    }
    _write_json(PLUGIN_ROOT / MODULE / "identity.json", identity)
    for descriptor_name in ("descriptor.json", "descriptor-parse.json"):
        path = PLUGIN_ROOT / descriptor_name
        descriptor = json.loads(path.read_bytes().decode("utf-8"))
        descriptor["provider"]["release_id"] = digest.release_id
        _write_json(path, descriptor)
    expected = {
        "schema": "source-import-package-expected/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "package_files": _package_files(),
        "files_sha256": digest.files_sha256.decode("utf-8"),
        "descriptor_sha256": hashlib.sha256((PLUGIN_ROOT / "descriptor.json").read_bytes()).hexdigest(),
        "descriptor_parse_sha256": hashlib.sha256((PLUGIN_ROOT / "descriptor-parse.json").read_bytes()).hexdigest(),
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
        "wheel_path": wheel_name,
        "wheel_import": MODULE,
        "input_schema_paths": [f"{MODULE}/schemas/inspect-input.schema.json", f"{MODULE}/schemas/parse-input.schema.json"],
        "output_schema_paths": [f"{MODULE}/schemas/inspect-output.schema.json", f"{MODULE}/schemas/parse-output.schema.json"],
        "descriptor_paths": ["descriptor.json", "descriptor-parse.json"],
        "plugin_path": "plugin.json",
        "schema_index_paths": {
            "source.import.inspect/v1": f"{MODULE}/schemas/inspect-index.json",
            "source.import.parse/v1": f"{MODULE}/schemas/parse-index.json",
        },
    }
    _write_json(PLUGIN_ROOT / "expected.json", expected)
    return expected


if __name__ == "__main__":
    value = rebuild()
    print(json.dumps({key: value[key] for key in ("plugin_id", "package_hash", "release_id", "wheel_path")}, sort_keys=True))
