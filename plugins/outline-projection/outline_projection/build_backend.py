"""Deterministic wheel and outer package builder."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from .capability_spec import (
    CAPABILITY_ID, PLUGIN_ID, VERSION, capability_projection, descriptor,
    plugin_manifest, ui_metadata,
)

NAME = "plotpilot_outline_projection"
MODULE = "outline_projection"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"
WHEEL_NAME = f"{NAME}-{VERSION}-py3-none-any.whl"
INDEX_PATH = f"{MODULE}/schemas/render/index.json"


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _metadata() -> dict[str, bytes]:
    return {
        f"{DIST_INFO}/METADATA": (
            "Metadata-Version: 2.3\nName: plotpilot-outline-projection\nVersion: 0.1.0\n"
            "Summary: Deterministic read-only outline projections\nRequires-Python: >=3.12,<3.13\n\n"
        ).encode(),
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nGenerator: nap03-deterministic-wheel-builder/1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{DIST_INFO}/top_level.txt": b"outline_projection\n",
    }


def build_canonical_wheel(root: Path, output_dir: Path) -> Path:
    files: dict[str, bytes] = {}
    # The installed wheel must be self-contained: runtime identity replay
    # needs the manifest, expected sidecar, descriptors, schemas and plugin
    # metadata without reaching back into a source checkout.  The generated
    # wheel is deliberately excluded from its own payload (and from the
    # package digest) to avoid a recursive wheel/identity dependency.
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode()):
        if (path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts
                and path.suffix != ".whl"):
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    files.update(_metadata())
    rows = []
    for name in sorted(files, key=lambda value: value.encode()):
        data = files[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([name, "sha256=" + digest, str(len(data))])
    rows.append([f"{DIST_INFO}/RECORD", "", ""])
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    files[f"{DIST_INFO}/RECORD"] = stream.getvalue().encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / WHEEL_NAME
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files, key=lambda value: value.encode()):
            archive.writestr(_entry(name), files[name])
    return output


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return build_canonical_wheel(Path(__file__).resolve().parents[1], Path(wheel_directory)).name


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    target = Path(metadata_directory) / DIST_INFO
    target.mkdir(parents=True, exist_ok=True)
    for name, data in _metadata().items():
        (target / name.split("/", 1)[1]).write_bytes(data)
    return DIST_INFO


def _payload(root: Path) -> dict[str, bytes]:
    excluded = {"files.sha256", "expected.json", f"{MODULE}/identity.json", ".gitattributes", "descriptor.json", "backend/plotpilot_outline_projection-0.1.0-py3-none-any.whl"}
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode()):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in excluded:
            result[relative] = path.read_bytes()
    return result


def rebuild_package() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    sdk = root.parents[1] / "sdk"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from plotpilot_plugin_sdk.package import digest_package

    _json(root / "plugin.json", plugin_manifest())
    _json(root / "ui" / "metadata-only.json", ui_metadata())
    schemas = []
    for schema_id, relative in (
        ("outline.projection.render-request/v1", "schemas/render/input.schema.json"),
        ("outline.projection.render-result/v1", "schemas/render/result.schema.json"),
    ):
        path = root / MODULE / relative
        schemas.append({"schema_id": schema_id, "path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    index = {"schema": "provider-schema-index/v1", "plugin_id": PLUGIN_ID, "capability_id": CAPABILITY_ID, "schemas": schemas}
    _json(root / INDEX_PATH, index)
    digest = digest_package(_payload(root), PLUGIN_ID, VERSION)
    identity = {"schema": "outline-projection-package-identity/v1", "plugin_id": PLUGIN_ID,
                "version": VERSION, "package_hash": digest.package_hash, "release_id": digest.release_id}
    _json(root / MODULE / "identity.json", identity)
    _json(root / "descriptor.json", descriptor(digest.release_id))
    (root / "files.sha256").write_bytes(digest.files_sha256)
    physical = sorted(
        [path.relative_to(root).as_posix() for path in root.rglob("*")
          if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and path.suffix != ".whl"
         and path.name not in {".gitattributes", "expected.json"}], key=lambda value: value.encode()
    )
    projection = capability_projection()
    schema_paths = [INDEX_PATH]
    schema_artifacts = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / MODULE / "schemas").rglob("*.schema.json"))
    }
    wheel_path = root / "backend" / WHEEL_NAME
    expected = {
        "schema": "outline-projection-package-expected/v1", "plugin_id": PLUGIN_ID, "version": VERSION,
        "package_files": physical, "files_sha256": digest.files_sha256.decode(),
        "package_hash": digest.package_hash, "release_id": digest.release_id, "provider_identity": identity,
        "capability_projection": projection,
        "capability_projection_sha256": hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "descriptor_paths": ["descriptor.json"],
        "descriptor_sha256": {"descriptor.json": hashlib.sha256((root / "descriptor.json").read_bytes()).hexdigest()},
        "descriptor_provider_identity": {"descriptor.json": descriptor(digest.release_id)["provider"]},
        "schema_index_paths": {CAPABILITY_ID: INDEX_PATH}, "schema_index_path_list": schema_paths,
        "schema_index_sha256": {INDEX_PATH: hashlib.sha256((root / INDEX_PATH).read_bytes()).hexdigest()},
        "schema_artifacts": {INDEX_PATH: schemas}, "schema_artifact_sha256": schema_artifacts,
        "wheel_path": wheel_path.relative_to(root).as_posix(), "wheel_import": MODULE,
        "plugin_path": "plugin.json", "identity_path": f"{MODULE}/identity.json",
    }
    _json(root / "expected.json", expected)
    # Build the wheel only after every sidecar has its final canonical bytes.
    # Since wheels are excluded from the digest/manifest, this does not create
    # a circular identity dependency and is byte-stable on a fresh rebuild.
    wheel = build_canonical_wheel(root, root / "backend")
    from .package_identity import calculate_identity
    verified = calculate_identity(root)
    if verified.package_hash != digest.package_hash or verified.release_id != digest.release_id:
        raise ValueError("rebuilt package identity did not verify")
    return expected
