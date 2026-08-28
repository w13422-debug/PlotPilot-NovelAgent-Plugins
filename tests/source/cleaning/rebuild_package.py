"""Rebuild the cleaning package with a deterministic wheel and SDK digests."""
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


PLUGIN_ROOT = ROOT / "plugins/source-cleaning-runtime"
MODULE = "source_cleaning_runtime"
PLUGIN_ID = "com.plotpilot.novelagent.source-cleaning-runtime"
VERSION = "0.1.0"
DIST = "plotpilot_source_cleaning_runtime"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
GENERATED = {"descriptor.json", "descriptor-apply.json", "descriptor-merge.json", "files.sha256", "expected.json", f"{MODULE}/identity.json"}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


def _wheel_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def _install_payload_files() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted((PLUGIN_ROOT / MODULE).rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json"} and path.name != "identity.json" and "__pycache__" not in path.parts:
            result[path.relative_to(PLUGIN_ROOT).as_posix()] = path.read_bytes()
    return result


def _write_install_identity() -> None:
    digest = digest_package(_install_payload_files(), PLUGIN_ID, VERSION)
    _write_json(
        PLUGIN_ROOT / MODULE / "identity.json",
        {"schema": "source-plugin-package-identity/v1", "plugin_id": PLUGIN_ID, "version": VERSION, "package_hash": digest.package_hash, "release_id": digest.release_id},
    )


def _build_wheel() -> Path:
    output = PLUGIN_ROOT / "backend" / f"{DIST}-{VERSION}-py3-none-any.whl"
    files: dict[str, bytes] = {}
    for path in sorted((PLUGIN_ROOT / MODULE).rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json"} and path.name != "identity.json" and "__pycache__" not in path.parts:
            files[path.relative_to(PLUGIN_ROOT).as_posix()] = path.read_bytes()
    files[f"{DIST_INFO}/METADATA"] = (
        "Metadata-Version: 2.3\nName: plotpilot-source-cleaning-runtime\nVersion: 0.1.0\n"
        "Summary: PlotPilot Novel-Agent deterministic source cleaning runtime\n"
        "Requires-Python: >=3.12,<3.13\n\n"
    ).encode("utf-8")
    files[f"{DIST_INFO}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: nap00-deterministic-wheel-builder/1.0\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode("ascii")
    files[f"{DIST_INFO}/top_level.txt"] = b"source_cleaning_runtime\n"
    rows = [[name, _wheel_hash(files[name]), str(len(files[name]))] for name in sorted(files)]
    rows.append([f"{DIST_INFO}/RECORD", "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files[f"{DIST_INFO}/RECORD"] = record.getvalue().encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            archive.writestr(_entry(name), files[name])
    return output


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


def _update_schema_index() -> None:
    schema_dir = PLUGIN_ROOT / MODULE / "schemas"
    def index(capability_id: str, pairs: tuple[tuple[str, str], ...]) -> dict[str, object]:
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
                for schema_id, path in pairs
            ],
        }

    indexes = {
        "preview-index.json": index(
            "source.clean.preview/v1",
            (
                ("source.clean.preview-request/v1", "schemas/preview-request.schema.json"),
                ("source.clean.preview-result/v1", "schemas/preview-result.schema.json"),
            ),
        ),
        "apply-index.json": index(
            "source.clean.apply/v1",
            (
                ("source.clean.apply-request/v1", "schemas/apply-request.schema.json"),
                ("source.clean.apply-result/v1", "schemas/apply-result.schema.json"),
            ),
        ),
        "merge-index.json": index(
            "source.clean.rules.merge/v1",
            (
                ("source.clean.rules.merge-request/v1", "schemas/merge-request.schema.json"),
                ("source.clean.rules.merge-result/v1", "schemas/merge-result.schema.json"),
            ),
        ),
    }
    for name, value in indexes.items():
        _write_json(schema_dir / name, value)


def rebuild() -> dict[str, object]:
    _update_schema_index()
    # The embedded identity is an install-scope sidecar derived without itself;
    # the outer package identity is derived from the final wheel bytes below.
    _write_install_identity()
    wheel = _build_wheel()
    digest = digest_package(_canonical_files(), PLUGIN_ID, VERSION)
    wheel_name = wheel.relative_to(PLUGIN_ROOT).as_posix()
    (PLUGIN_ROOT / "files.sha256").write_bytes(digest.files_sha256)
    identity = {"schema": "source-plugin-package-identity/v1", "plugin_id": PLUGIN_ID, "version": VERSION, "package_hash": digest.package_hash, "release_id": digest.release_id}
    _write_json(PLUGIN_ROOT / MODULE / "identity.json", identity)
    for descriptor_name in ("descriptor.json", "descriptor-apply.json", "descriptor-merge.json"):
        path = PLUGIN_ROOT / descriptor_name
        descriptor = json.loads(path.read_bytes().decode("utf-8"))
        descriptor["provider"]["release_id"] = digest.release_id
        _write_json(path, descriptor)
    expected = {
        "schema": "source-cleaning-package-expected/v1",
        "plugin_id": PLUGIN_ID,
        "version": VERSION,
        "package_files": _package_files(),
        "files_sha256": digest.files_sha256.decode("utf-8"),
        "descriptor_sha256": hashlib.sha256((PLUGIN_ROOT / "descriptor.json").read_bytes()).hexdigest(),
        "descriptor_apply_sha256": hashlib.sha256((PLUGIN_ROOT / "descriptor-apply.json").read_bytes()).hexdigest(),
        "descriptor_merge_sha256": hashlib.sha256((PLUGIN_ROOT / "descriptor-merge.json").read_bytes()).hexdigest(),
        "descriptor_paths": ["descriptor.json", "descriptor-apply.json", "descriptor-merge.json"],
        "input_schema_paths": [f"{MODULE}/schemas/preview-request.schema.json", f"{MODULE}/schemas/apply-request.schema.json", f"{MODULE}/schemas/merge-request.schema.json"],
        "output_schema_paths": [f"{MODULE}/schemas/preview-result.schema.json", f"{MODULE}/schemas/apply-result.schema.json", f"{MODULE}/schemas/merge-result.schema.json"],
        "plugin_path": "plugin.json",
        "package_hash": digest.package_hash,
        "release_id": digest.release_id,
        "wheel_path": wheel_name,
        "wheel_import": MODULE,
        "schema_index_paths": {
            "source.clean.preview/v1": f"{MODULE}/schemas/preview-index.json",
            "source.clean.apply/v1": f"{MODULE}/schemas/apply-index.json",
            "source.clean.rules.merge/v1": f"{MODULE}/schemas/merge-index.json",
        },
    }
    _write_json(PLUGIN_ROOT / "expected.json", expected)
    return expected


if __name__ == "__main__":
    value = rebuild()
    print(json.dumps({key: value[key] for key in ("plugin_id", "package_hash", "release_id", "wheel_path")}, sort_keys=True))
