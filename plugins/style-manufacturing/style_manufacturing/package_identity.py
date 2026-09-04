"""Package identity verification for the generated style-manufacturing bundle."""
from __future__ import annotations
import json
import hashlib
import re
from pathlib import Path
from typing import Any

from .capability_spec import (
    DESCRIPTOR_PATHS, PLUGIN_ID, SPEC_BY_CAPABILITY, VERSION,
    capability_projection, plugin_manifest, ui_metadata,
)

MODULE_NAME = "style_manufacturing"
IDENTITY_SCHEMA = "style-manufacturing-package-identity/v1"
_FIELDS = frozenset({"schema", "plugin_id", "version", "package_hash", "release_id"})
_EXPECTED_FIELDS = frozenset({
    "schema", "plugin_id", "version", "package_files", "files_sha256", "package_hash",
    "release_id", "provider_identity", "descriptor_paths", "descriptor_sha256",
    "descriptor_provider_identity", "schema_index_paths", "schema_index_path_list",
    "schema_index_sha256", "schema_artifacts", "schema_artifact_sha256", "wheel_path",
    "wheel_import", "plugin_path", "identity_path", "capability_projection",
    "capability_projection_sha256",
})
_LINE = re.compile(r"^[0-9a-f]{64}  (.+)\n$")
_SPEC_BY_DESCRIPTOR = {spec.descriptor_path: spec for spec in SPEC_BY_CAPABILITY.values()}
_DESCRIPTOR_FIELDS = frozenset({
    "schema", "capability_id", "provider", "input_schema", "output_schema",
    "result_contract", "supports", "deterministic", "accepted_data_formats",
})

def package_root() -> Path: return Path(__file__).resolve().parents[1]
def identity_path(root: Path | None = None) -> Path: return (root or package_root()) / MODULE_NAME / "identity.json"

def _read_identity(root: Path | None = None) -> dict[str, Any]:
    try: value = json.loads(identity_path(root).read_text(encoding="utf-8"))
    except Exception as exc: raise RuntimeError("style-manufacturing package identity is unavailable") from exc
    if not isinstance(value, dict) or set(value) != set(_FIELDS): raise RuntimeError("style-manufacturing package identity schema is invalid")
    if value["schema"] != IDENTITY_SCHEMA or value["plugin_id"] != PLUGIN_ID or value["version"] != VERSION: raise RuntimeError("style-manufacturing package identity owner/version is invalid")
    for field in ("package_hash", "release_id"):
        if (not isinstance(value[field], str) or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None
                or value[field] == "0" * 64):
            raise RuntimeError("style-manufacturing package identity hash is invalid")
    return value

def _read_json(path: Path, label: str) -> dict[str, Any]:
    def reject_duplicate(pairs):
        result = {}
        for key, item in pairs:
            if key in result: raise ValueError("duplicate key")
            result[key] = item
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate,
                           parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except Exception as exc:
        raise RuntimeError(f"style-manufacturing {label} is unavailable or invalid") from exc
    if not isinstance(value, dict): raise RuntimeError(f"style-manufacturing {label} is invalid")
    return value

def _safe_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RuntimeError(f"style-manufacturing {label} path is invalid")
    path = root / relative
    try: path.resolve().relative_to(root.resolve())
    except ValueError as exc: raise RuntimeError(f"style-manufacturing {label} escapes package") from exc
    if not path.is_file(): raise RuntimeError(f"style-manufacturing {label} is missing: {relative}")
    return path

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _physical_package_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        if (not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc"
                or path.name in {".gitattributes", "expected.json"}):
            continue
        files.append(path.relative_to(root).as_posix())
    return sorted(files, key=lambda value: value.encode())

def _verify_descriptor_semantics(relative: str, descriptor: dict[str, Any], provider: dict[str, str]) -> None:
    spec = _SPEC_BY_DESCRIPTOR.get(relative)
    if spec is None or set(descriptor) != set(_DESCRIPTOR_FIELDS):
        raise RuntimeError("style-manufacturing descriptor path/schema is not frozen")
    expected = spec.descriptor(provider["release_id"])
    for field, expected_value in expected.items():
        if descriptor.get(field) != expected_value:
            raise RuntimeError(
                f"style-manufacturing descriptor {field} does not match canonical runtime semantics"
            )

def _verify_expected(root: Path, identity: dict[str, Any], manifest: bytes, digest: Any) -> None:
    expected = _read_json(root / "expected.json", "expected identity")
    if set(expected) != set(_EXPECTED_FIELDS) or expected.get("schema") != "style-manufacturing-package-expected/v1":
        raise RuntimeError("style-manufacturing expected identity schema is invalid")
    if expected.get("plugin_id") != PLUGIN_ID or expected.get("version") != VERSION:
        raise RuntimeError("style-manufacturing expected identity owner/version is invalid")
    if (expected.get("package_hash") != digest.package_hash
            or expected.get("release_id") != digest.release_id
            or expected.get("files_sha256") != manifest.decode("utf-8")
            or expected.get("provider_identity") != identity):
        raise RuntimeError("style-manufacturing expected identity does not match SDK digest")

    projection = capability_projection()
    projection_hash = hashlib.sha256(json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if (expected.get("capability_projection") != projection
            or expected.get("capability_projection_sha256") != projection_hash):
        raise RuntimeError("style-manufacturing expected capability projection is not canonical")
    if _read_json(root / "plugin.json", "plugin manifest") != plugin_manifest():
        raise RuntimeError("style-manufacturing plugin manifest differs from capability specification")
    if _read_json(root / "ui" / "metadata-only.json", "UI metadata") != ui_metadata():
        raise RuntimeError("style-manufacturing UI projection differs from capability specification")

    declared_files = expected.get("package_files")
    if (not isinstance(declared_files, list) or declared_files != _physical_package_files(root)
            or len(declared_files) != len(set(declared_files))):
        raise RuntimeError("style-manufacturing package inventory is not exact")

    descriptor_paths = expected.get("descriptor_paths")
    descriptor_hashes = expected.get("descriptor_sha256")
    descriptor_identities = expected.get("descriptor_provider_identity")
    if (descriptor_paths != list(DESCRIPTOR_PATHS)
            or set(descriptor_hashes or {}) != set(descriptor_paths)
            or set(descriptor_identities or {}) != set(descriptor_paths)):
        raise RuntimeError("style-manufacturing descriptor identity index is invalid")
    provider = {"plugin_id": PLUGIN_ID, "release_id": digest.release_id}
    for relative in descriptor_paths:
        path = _safe_file(root, relative, "descriptor")
        descriptor = _read_json(path, "descriptor")
        _verify_descriptor_semantics(relative, descriptor, provider)
        if (_sha256(path) != descriptor_hashes[relative]
                or descriptor.get("provider") != provider
                or descriptor_identities[relative] != provider):
            raise RuntimeError("style-manufacturing descriptor identity does not match expected identity")

    index_paths = expected.get("schema_index_path_list")
    index_by_capability = expected.get("schema_index_paths")
    index_hashes = expected.get("schema_index_sha256")
    artifacts_by_index = expected.get("schema_artifacts")
    artifact_hashes = expected.get("schema_artifact_sha256")
    if (not isinstance(index_paths, list) or not index_paths
            or set(index_by_capability.values() if isinstance(index_by_capability, dict) else ()) != set(index_paths)
            or set(index_hashes or {}) != set(index_paths)
            or set(artifacts_by_index or {}) != set(index_paths)
            or not isinstance(artifact_hashes, dict)):
        raise RuntimeError("style-manufacturing schema identity index is invalid")
    observed_artifacts: dict[str, str] = {}
    module_root = root / MODULE_NAME
    for relative in artifact_hashes:
        artifact = _safe_file(root, relative, "schema artifact")
        observed_artifacts[relative] = _sha256(artifact)
    if observed_artifacts != artifact_hashes:
        raise RuntimeError("style-manufacturing schema artifacts do not match expected identity")
    for relative in index_paths:
        path = _safe_file(root, relative, "schema index")
        index = _read_json(path, "schema index")
        if _sha256(path) != index_hashes[relative] or index.get("schemas") != artifacts_by_index[relative]:
            raise RuntimeError("style-manufacturing schema index does not match expected identity")
        for item in index.get("schemas", []):
            if not isinstance(item, dict) or set(item) != {"schema_id", "path", "sha256"}:
                raise RuntimeError("style-manufacturing schema artifact entry is invalid")
            artifact = _safe_file(module_root, item["path"], "schema artifact")
            artifact_relative = artifact.relative_to(root).as_posix()
            actual = _sha256(artifact)
            if item["sha256"] != actual or artifact_hashes.get(artifact_relative) != actual:
                raise RuntimeError("style-manufacturing schema artifact hash is invalid")

def _manifest(root: Path) -> tuple[tuple[str, ...], bytes]:
    raw = (root / "files.sha256").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n") or raw.endswith(b"\n\n"): raise RuntimeError("style-manufacturing manifest must be UTF-8 LF with one final newline")
    names: list[str] = []
    for line in raw.decode("utf-8").splitlines(keepends=True):
        match = _LINE.fullmatch(line)
        if match is None: raise RuntimeError("style-manufacturing manifest line is malformed")
        name = match.group(1)
        if name in names or name == "files.sha256": raise RuntimeError("style-manufacturing manifest has duplicate/self entry")
        names.append(name)
    if not names: raise RuntimeError("style-manufacturing manifest is empty")
    return tuple(names), raw

def canonical_payload_files(root: Path | None = None) -> dict[str, bytes]:
    bundle = root or package_root(); names, _ = _manifest(bundle); resolved = bundle.resolve(); result: dict[str, bytes] = {}
    for name in names:
        path = bundle / name
        try: path.resolve().relative_to(resolved)
        except ValueError as exc: raise RuntimeError("style-manufacturing manifest escapes package") from exc
        if not path.is_file(): raise RuntimeError("style-manufacturing manifest input is missing: " + name)
        result[name] = path.read_bytes()
    return result

def calculate_identity(root: Path | None = None):
    from plotpilot_plugin_sdk.package import digest_package
    bundle = root or package_root(); value = _read_identity(bundle); names, manifest = _manifest(bundle); files = canonical_payload_files(bundle)
    if tuple(files) != names: raise RuntimeError("style-manufacturing manifest order mismatch")
    digest = digest_package(files, PLUGIN_ID, VERSION)
    if digest.files_sha256 != manifest: raise RuntimeError("style-manufacturing files.sha256 does not match SDK digest")
    if value["package_hash"] != digest.package_hash or value["release_id"] != digest.release_id: raise RuntimeError("style-manufacturing identity does not match SDK digest")
    _verify_expected(bundle, value, manifest, digest)
    return digest

def load_runtime_identity() -> dict[str, Any]:
    value = _read_identity(); manifest = package_root() / "files.sha256"
    if manifest.is_file():
        try: calculate_identity(package_root())
        except ModuleNotFoundError as exc:
            if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"): raise
    return value
