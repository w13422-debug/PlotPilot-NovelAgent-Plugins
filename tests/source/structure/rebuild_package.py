"""Build and bind the NAP-01 source-structure package deterministically."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.package import digest_package  # noqa: E402

PLUGIN_ID = "com.plotpilot.novelagent.source-structure"
VERSION = "0.1.0"
MODULE = "source_structure"
DISTRIBUTION = "plotpilot_source_structure"
PACKAGE_ROOT = ROOT / "plugins" / "source-structure"
DESCRIPTOR_PATHS = [
    "descriptor.json",
    "descriptor-rebind-inspect.json",
    "descriptor-rebind-propose.json",
    "descriptor-search.json",
]
SCHEMA_INDEX_BY_CAPABILITY = {
    "source.structure.revise/v1": "source_structure/schemas/revise/index.json",
    "source.evidence.search/v1": "source_structure/schemas/search/index.json",
    "source.evidence.rebind.inspect/v1": "source_structure/schemas/rebind-inspect/index.json",
    "source.evidence.rebind.propose/v1": "source_structure/schemas/rebind-propose/index.json",
}
INDEX_PATHS = [
    "source_structure/schemas/rebind-inspect/index.json",
    "source_structure/schemas/rebind-propose/index.json",
    "source_structure/schemas/search/index.json",
    "source_structure/schemas/revise/index.json",
]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wheel_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def _wheel_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def _update_schema_indexes() -> dict[str, list[dict[str, str]]]:
    bindings: dict[str, list[dict[str, str]]] = {}
    for relative in INDEX_PATHS:
        path = PACKAGE_ROOT / relative
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"schema", "plugin_id", "capability_id", "schemas"}:
            raise ValueError(f"schema index is not closed: {relative}")
        if value["schema"] != "provider-schema-index/v1" or value["plugin_id"] != PLUGIN_ID:
            raise ValueError(f"schema index identity mismatch: {relative}")
        entries = value["schemas"]
        if not isinstance(entries, list) or len(entries) != 2:
            raise ValueError(f"schema index must bind exactly request/result: {relative}")
        normalized: list[dict[str, str]] = []
        for entry in entries:
            if set(entry) != {"schema_id", "path", "sha256"}:
                raise ValueError(f"schema index entry is not closed: {relative}")
            schema_path = PACKAGE_ROOT / MODULE / entry["path"]
            if not schema_path.is_file():
                raise ValueError(f"schema artifact is missing: {entry['path']}")
            digest = _sha256(schema_path.read_bytes())
            entry["sha256"] = digest
            normalized.append({"schema_id": entry["schema_id"], "path": entry["path"], "sha256": digest})
        _write_json(path, value)
        bindings[relative] = normalized
    return bindings


def _build_wheel() -> Path:
    output = PACKAGE_ROOT / "backend" / f"{DISTRIBUTION}-{VERSION}-py3-none-any.whl"
    files: dict[str, bytes] = {}
    package_root = PACKAGE_ROOT / MODULE
    for path in sorted(package_root.rglob("*"), key=lambda item: item.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in {".py", ".json"} or path.name == "identity.json":
            continue
        files[path.relative_to(PACKAGE_ROOT).as_posix()] = path.read_bytes()
    dist_info = f"{DISTRIBUTION}-{VERSION}.dist-info"
    files[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.3\n"
        f"Name: {DISTRIBUTION.replace('_', '-')}\n"
        f"Version: {VERSION}\n"
        "Summary: Novel-Agent source structure and evidence\n"
        "Requires-Python: >=3.12,<3.13\n\n"
    ).encode("utf-8")
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: nap00-deterministic-wheel-builder/1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist_info}/top_level.txt"] = f"{MODULE}\n".encode("ascii")
    record_rows: list[list[str]] = []
    for name in sorted(files, key=lambda item: item.encode("utf-8")):
        data = files[name]
        record_rows.append([name, _wheel_hash(data), str(len(data))])
    record_rows.append([f"{dist_info}/RECORD", "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(record_rows)
    files[f"{dist_info}/RECORD"] = record.getvalue().encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files, key=lambda item: item.encode("utf-8")):
            archive.writestr(_wheel_entry(name), files[name])
    return output


def _canonical_payload_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*"), key=lambda item: item.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in {"files.sha256", "expected.json", f"{MODULE}/identity.json", ".gitattributes"} or relative in set(DESCRIPTOR_PATHS):
            continue
        files[relative] = path.read_bytes()
    return files


def _package_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*"), key=lambda item: item.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc" or path.name in {".gitattributes", "expected.json", "files.sha256"}:
            continue
        files[path.relative_to(PACKAGE_ROOT).as_posix()] = path.read_bytes()
    return files


def rebuild() -> dict[str, object]:
    schema_bindings = _update_schema_indexes()
    wheel = _build_wheel()
    payload = _canonical_payload_files()
    digest = digest_package(payload, PLUGIN_ID, VERSION)
    manifest_names = [line[:-1].split("  ", 1)[1] for line in digest.files_sha256.decode("utf-8").splitlines(keepends=True)]
    if set(manifest_names) != set(payload):
        raise ValueError("public SDK manifest does not describe the canonical payload")
    if any(name.startswith("descriptors/") or name in {"files.sha256", f"{MODULE}/identity.json"} for name in manifest_names):
        raise ValueError("derived descriptors/identity must stay outside canonical payload")
    wheel_relative = wheel.relative_to(PACKAGE_ROOT).as_posix()
    if wheel_relative not in manifest_names:
        raise ValueError("installable wheel is absent from canonical payload")

    identity = {
        "schema": "source-structure-package-identity/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
    }
    _write_json(PACKAGE_ROOT / MODULE / "identity.json", identity)
    descriptor_hashes: dict[str, str] = {}
    descriptor_identities: dict[str, dict[str, str]] = {}
    for relative in DESCRIPTOR_PATHS:
        path = PACKAGE_ROOT / relative
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        descriptor["provider"]["release_id"] = digest.release_id
        _write_json(path, descriptor)
        descriptor_hashes[relative] = _sha256(path.read_bytes())
        descriptor_identities[relative] = dict(descriptor["provider"])

    PACKAGE_ROOT.joinpath("files.sha256").write_bytes(digest.files_sha256)
    physical = _package_files()
    package_files = sorted([*physical, "files.sha256"], key=lambda item: item.encode("utf-8"))
    schema_artifacts = {relative: entries for relative, entries in sorted(schema_bindings.items())}
    schema_index_hashes = {relative: _sha256((PACKAGE_ROOT / relative).read_bytes()) for relative in INDEX_PATHS}
    schema_artifact_hashes = {
        path.relative_to(PACKAGE_ROOT).as_posix(): _sha256(path.read_bytes())
        for path in sorted((PACKAGE_ROOT / MODULE / "schemas").rglob("*.schema.json"), key=lambda item: item.relative_to(PACKAGE_ROOT).as_posix().encode("utf-8"))
    }
    expected = {
        "schema": "source-structure-package-expected/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "package_files": package_files,
        "files_sha256": digest.files_sha256.decode("utf-8"),
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
        "provider_identity": identity,
        "descriptor_paths": DESCRIPTOR_PATHS,
        "descriptor_sha256": descriptor_hashes,
        "descriptor_provider_identity": descriptor_identities,
        "descriptor_path": "descriptor.json",
        "schema_index_paths": SCHEMA_INDEX_BY_CAPABILITY,
        "schema_index_path_list": INDEX_PATHS,
        "schema_index_sha256": schema_index_hashes,
        "schema_artifacts": schema_artifacts,
        "schema_artifact_sha256": schema_artifact_hashes,
        "wheel_path": wheel_relative,
        "wheel_import": MODULE,
        "plugin_path": "plugin.json",
        "identity_path": f"{MODULE}/identity.json",
    }
    _write_json(PACKAGE_ROOT / "expected.json", expected)
    return expected


def main() -> None:
    expected = rebuild()
    print(json.dumps({"plugin_id": PLUGIN_ID, "package_hash": expected["package_hash"], "release_id": expected["release_id"], "wheel_path": expected["wheel_path"]}, sort_keys=True))


if __name__ == "__main__":
    main()