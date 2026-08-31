"""Strict identity replay for the outline projection package."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

from .capability_spec import (
    CAPABILITY_ID,
    PLUGIN_ID,
    VERSION,
    capability_projection,
    descriptor,
    plugin_manifest,
    ui_metadata,
)

MODULE = "outline_projection"
_LINE = re.compile(r"^([0-9a-f]{64})  ([^\\\r\n]+)$")


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _manifest(root: Path) -> tuple[list[str], bytes]:
    raw = (root / "files.sha256").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n"):
        raise RuntimeError("outline projection manifest encoding is invalid")
    names = []
    for line in raw.decode().splitlines():
        match = _LINE.fullmatch(line)
        if match is None or match.group(2) in names or match.group(2) == "files.sha256":
            raise RuntimeError("outline projection manifest is malformed")
        path = root / match.group(2)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != match.group(1):
            raise RuntimeError("outline projection package payload differs")
        names.append(match.group(2))
    return names, raw


def calculate_identity(root: Path | None = None):
    bundle = root or package_root()
    sdk = bundle.parents[1] / "sdk"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from plotpilot_plugin_sdk.package import digest_package
    names, raw = _manifest(bundle)
    files = {name: (bundle / name).read_bytes() for name in names}
    digest = digest_package(files, PLUGIN_ID, VERSION)
    identity = json.loads((bundle / MODULE / "identity.json").read_text(encoding="utf-8"))
    expected = json.loads((bundle / "expected.json").read_text(encoding="utf-8"))
    if digest.files_sha256 != raw or identity != {"schema": "outline-projection-package-identity/v1", "plugin_id": PLUGIN_ID,
                                                  "version": VERSION, "package_hash": digest.package_hash, "release_id": digest.release_id}:
        raise RuntimeError("outline projection identity differs")
    if expected["package_hash"] != digest.package_hash or expected["release_id"] != digest.release_id:
        raise RuntimeError("outline projection expected identity differs")
    expected_fields = {
        "schema", "plugin_id", "version", "package_files", "files_sha256",
        "package_hash", "release_id", "provider_identity", "capability_projection",
        "capability_projection_sha256", "descriptor_paths", "descriptor_sha256",
        "descriptor_provider_identity", "schema_index_paths", "schema_index_path_list",
        "schema_index_sha256", "schema_artifacts", "schema_artifact_sha256",
        "wheel_path", "wheel_import", "plugin_path", "identity_path",
    }
    if (set(expected) != expected_fields
            or expected["schema"] != "outline-projection-package-expected/v1"
            or expected["plugin_id"] != PLUGIN_ID or expected["version"] != VERSION
            or expected["provider_identity"] != identity
            or expected["files_sha256"].encode() != raw):
        raise RuntimeError("outline projection expected identity schema differs")
    # A wheel is installed alongside its ``*.dist-info`` metadata directory.
    # That metadata is produced by the installer, is not part of the plugin
    # payload, and must not make the wheel-local inventory differ from the
    # source checkout's expected sidecar.  Ignore only dist-info directories
    # (never arbitrary files) so all plugin payload bytes remain covered.
    physical = sorted(
        [path.relative_to(bundle).as_posix() for path in bundle.rglob("*")
         if path.is_file() and "__pycache__" not in path.parts
         and not any(part.endswith(".dist-info") for part in path.parts)
         and path.suffix not in {".pyc", ".whl"}
         and path.name not in {".gitattributes", "expected.json"}],
        key=lambda value: value.encode(),
    )
    if expected["package_files"] != physical:
        raise RuntimeError("outline projection physical inventory differs")
    if json.loads((bundle / "plugin.json").read_text(encoding="utf-8")) != plugin_manifest():
        raise RuntimeError("outline projection manifest differs from frozen spec")
    descriptor_path = bundle / "descriptor.json"
    descriptor_value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    provider = {"plugin_id": PLUGIN_ID, "release_id": digest.release_id}
    if (expected["descriptor_paths"] != ["descriptor.json"]
            or expected["descriptor_sha256"] != {"descriptor.json": hashlib.sha256(descriptor_path.read_bytes()).hexdigest()}
            or expected["descriptor_provider_identity"] != {"descriptor.json": provider}
            or descriptor_value != descriptor(digest.release_id)):
        raise RuntimeError("outline projection descriptor differs from frozen spec")
    if json.loads((bundle / "ui" / "metadata-only.json").read_text(encoding="utf-8")) != ui_metadata():
        raise RuntimeError("outline projection UI metadata differs from frozen spec")
    projection = capability_projection()
    projection_hash = hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (expected["capability_projection"] != projection
            or expected["capability_projection_sha256"] != projection_hash):
        raise RuntimeError("outline projection capability projection differs")
    index_path = "outline_projection/schemas/render/index.json"
    if (expected["schema_index_paths"] != {CAPABILITY_ID: index_path}
            or expected["schema_index_path_list"] != [index_path]):
        raise RuntimeError("outline projection schema index map differs")
    index_raw = (bundle / index_path).read_bytes()
    index = json.loads(index_raw)
    if (set(index) != {"schema", "plugin_id", "capability_id", "schemas"}
            or index.get("schema") != "provider-schema-index/v1"
            or index.get("plugin_id") != PLUGIN_ID
            or index.get("capability_id") != CAPABILITY_ID):
        raise RuntimeError("outline projection provider schema index identity differs")
    if (expected["schema_index_sha256"] != {index_path: hashlib.sha256(index_raw).hexdigest()}
            or expected["schema_artifacts"] != {index_path: index["schemas"]}):
        raise RuntimeError("outline projection schema index differs")
    observed_artifacts = {
        path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((bundle / MODULE / "schemas").rglob("*.schema.json"))
    }
    if expected["schema_artifact_sha256"] != observed_artifacts:
        raise RuntimeError("outline projection schema artifacts differ")
    for item in index["schemas"]:
        path = bundle / MODULE / item["path"]
        if (set(item) != {"schema_id", "path", "sha256"}
                or not isinstance(item["schema_id"], str)
                or item["schema_id"] != json.loads(path.read_text(encoding="utf-8")).get("$id")
                or item["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise RuntimeError("outline projection indexed schema hash differs")
    if (expected["wheel_path"] != "backend/plotpilot_outline_projection-0.1.0-py3-none-any.whl"
            or expected["wheel_import"] != MODULE or expected["plugin_path"] != "plugin.json"
            or expected["identity_path"] != f"{MODULE}/identity.json"):
        raise RuntimeError("outline projection install identity differs")
    return digest


def load_runtime_identity() -> dict[str, str]:
    """Load only an identity that can be independently replayed.

    Runtime code must not trust an installed sidecar merely because it is
    parseable: the manifest, schema indexes, descriptor, UI projection and
    package digest are the identity authority for this wheel.
    """
    root = package_root()
    try:
        digest = calculate_identity(root)
    except ModuleNotFoundError as exc:
        if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"):
            raise
        raise RuntimeError("outline projection public SDK is unavailable") from exc
    value = json.loads((root / MODULE / "identity.json").read_text(encoding="utf-8"))
    if value.get("package_hash") != digest.package_hash or value.get("release_id") != digest.release_id:
        raise RuntimeError("outline projection installed identity does not match package digest")
    return value
