"""Independent verifier for the immutable style-runtime package identity."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .capability_spec import (
    DESCRIPTOR_PATHS,
    INDEX_BY_CAPABILITY,
    PLUGIN_ID,
    SPEC_BY_CAPABILITY,
    VERSION,
    capability_projection,
    plugin_manifest,
    ui_metadata,
)

_LINE = re.compile(r"([0-9a-f]{64})  ([^\r\n]+)\n")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain an object")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> tuple[tuple[str, ...], bytes]:
    raw = (root / "files.sha256").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise RuntimeError("style-runtime manifest must be UTF-8 LF with one final newline")
    names: list[str] = []
    for line in raw.decode("utf-8").splitlines(keepends=True):
        match = _LINE.fullmatch(line)
        if match is None or match.group(2) in names or match.group(2) == "files.sha256":
            raise RuntimeError("style-runtime manifest line is malformed or duplicated")
        names.append(match.group(2))
    if not names:
        raise RuntimeError("style-runtime manifest is empty")
    return tuple(names), raw


def canonical_payload_files(root: Path | None = None) -> dict[str, bytes]:
    bundle = root or package_root()
    names, _ = _manifest(bundle)
    resolved = bundle.resolve()
    result: dict[str, bytes] = {}
    for name in names:
        path = bundle / name
        try:
            path.resolve().relative_to(resolved)
        except ValueError as exc:
            raise RuntimeError("style-runtime manifest escapes package") from exc
        if not path.is_file():
            raise RuntimeError("style-runtime manifest input is missing: " + name)
        result[name] = path.read_bytes()
    return result


def _physical_package_files(root: Path) -> list[str]:
    return sorted([
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and path.name not in {".gitattributes", "expected.json"}
    ], key=lambda value: value.encode())


def calculate_identity(root: Path | None = None):
    from plotpilot_plugin_sdk.package import digest_package

    bundle = root or package_root()
    identity = _object(bundle / "style_runtime" / "identity.json", "style-runtime identity")
    if set(identity) != {"schema", "package_kind", "plugin_id", "version", "package_hash", "release_id", "identity_separation"}:
        raise RuntimeError("style-runtime identity fields differ")
    if identity["schema"] != "style-runtime-package-identity/v1" or identity["package_kind"] != "code" or identity["plugin_id"] != PLUGIN_ID or identity["version"] != VERSION:
        raise RuntimeError("style-runtime identity namespace mismatch")
    domains = identity["identity_separation"]
    if domains != {
        "style_release_domain": "style-release/v1", "data_release_domain": "plotpilot-release/v1",
        "skill_release_domain": "plotpilot-skill-release/v1", "code_release_domain": "plotpilot-release/v1",
    }:
        raise RuntimeError("Style/Data/Skill/Code identity domains are not explicitly separated")
    if identity["release_id"] == identity["package_hash"] or _HASH.fullmatch(identity["release_id"]) is None:
        raise RuntimeError("style-runtime release identity is invalid")
    names, manifest = _manifest(bundle)
    files = canonical_payload_files(bundle)
    if tuple(files) != names:
        raise RuntimeError("style-runtime manifest order mismatch")
    digest = digest_package(files, PLUGIN_ID, VERSION)
    if digest.files_sha256 != manifest or identity["package_hash"] != digest.package_hash or identity["release_id"] != digest.release_id:
        raise RuntimeError("style-runtime identity does not match SDK digest")

    expected = _object(bundle / "expected.json", "style-runtime expected")
    if expected.get("package_hash") != digest.package_hash or expected.get("release_id") != digest.release_id or expected.get("files_sha256") != manifest.decode() or expected.get("provider_identity") != identity:
        raise RuntimeError("style-runtime expected identity differs")
    if expected.get("package_files") != _physical_package_files(bundle):
        raise RuntimeError("style-runtime package inventory is not exact")
    projection = capability_projection()
    projection_hash = hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if expected.get("capability_projection") != projection or expected.get("capability_projection_sha256") != projection_hash:
        raise RuntimeError("style-runtime capability projection differs")
    if _object(bundle / "plugin.json", "plugin manifest") != plugin_manifest():
        raise RuntimeError("style-runtime plugin manifest differs from specification")
    if _object(bundle / "ui" / "metadata-only.json", "UI metadata") != ui_metadata():
        raise RuntimeError("style-runtime UI metadata differs from specification")

    provider = {"plugin_id": PLUGIN_ID, "release_id": digest.release_id}
    if expected.get("descriptor_paths") != list(DESCRIPTOR_PATHS):
        raise RuntimeError("style-runtime descriptor paths differ")
    for relative in DESCRIPTOR_PATHS:
        descriptor = _object(bundle / relative, "descriptor")
        capability = descriptor.get("capability_id")
        if capability not in SPEC_BY_CAPABILITY or descriptor != SPEC_BY_CAPABILITY[capability].descriptor(digest.release_id):
            raise RuntimeError("style-runtime descriptor differs from frozen catalog")
        if descriptor["provider"] != provider or expected["descriptor_sha256"].get(relative) != _sha(bundle / relative):
            raise RuntimeError("style-runtime descriptor identity mismatch")

    if expected.get("schema_index_paths") != INDEX_BY_CAPABILITY:
        raise RuntimeError("style-runtime schema-index map differs")
    for capability, relative in INDEX_BY_CAPABILITY.items():
        index = _object(bundle / relative, "schema index")
        if set(index) != {"schema", "plugin_id", "capability_id", "schemas"} or index["schema"] != "plugin-schema-index/v1" or index["plugin_id"] != PLUGIN_ID or index["capability_id"] != capability:
            raise RuntimeError("style-runtime schema index is not closed")
        if expected["schema_index_sha256"].get(relative) != _sha(bundle / relative) or expected["schema_artifacts"].get(relative) != index["schemas"]:
            raise RuntimeError("style-runtime schema index identity mismatch")
        for item in index["schemas"]:
            if set(item) != {"schema_id", "path", "sha256"}:
                raise RuntimeError("style-runtime schema index item is not closed")
            artifact = bundle / "style_runtime" / item["path"]
            if not artifact.is_file() or item["sha256"] != _sha(artifact) or expected["schema_artifact_sha256"].get(artifact.relative_to(bundle).as_posix()) != item["sha256"]:
                raise RuntimeError("style-runtime schema artifact identity mismatch")
    return digest


def load_runtime_identity() -> dict[str, Any]:
    value = _object(package_root() / "style_runtime" / "identity.json", "style-runtime identity")
    if (package_root() / "files.sha256").is_file():
        try:
            calculate_identity(package_root())
        except ModuleNotFoundError as exc:
            if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"):
                raise
    return value
