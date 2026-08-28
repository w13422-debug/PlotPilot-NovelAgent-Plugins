"""Closed provider-local schema index and validator.

The public PlotPilot SDK remains the authority for shared contracts.  The
provider package additionally ships a strict, hashed schema index so a host
can resolve every descriptor reference from the package artifact itself,
without reaching into the source tree.  All index and artifact integrity
failures are deliberately fail-closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - exercised in the SDK-absent subprocess
    _JSONSCHEMA_IMPORT_ERROR: BaseException | None = exc
    Draft202012Validator = None  # type: ignore[assignment,misc]
else:
    _JSONSCHEMA_IMPORT_ERROR = None


class ProviderSchemaError(ValueError):
    """Schema index loading or validation failed before a provider side effect."""


SCHEMA_DIR = Path(__file__).with_name("schemas")
SCHEMA_INDEX_PATH = SCHEMA_DIR / "index.json"
PLUGIN_ID = "com.plotpilot.novelagent.provider-anthropic"
CAPABILITY_ID = "model.provider.anthropic.invoke/v1"
INPUT_SCHEMA = "model.provider.anthropic.invoke-request/v1"
OUTPUT_SCHEMA = "model.provider.anthropic.invoke-result/v1"
EXPECTED_SCHEMA_IDS = (INPUT_SCHEMA, OUTPUT_SCHEMA)

_INDEX_FIELDS = frozenset({"schema", "plugin_id", "capability_id", "schemas"})
_INDEX_ENTRY_FIELDS = frozenset({"schema_id", "path", "sha256"})
_SCHEMA_DOCUMENT_FIELDS = frozenset({"$id", "$schema", "type", "additionalProperties", "properties", "required", "$defs", "oneOf"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_without_duplicate_keys)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProviderSchemaError(f"{label} cannot be read as strict UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ProviderSchemaError(f"{label} must be a JSON object: {path}")
    return value


def _artifact_path(index_path: Path, raw_path: Any) -> Path:
    if type(raw_path) is not str or not raw_path:
        raise ProviderSchemaError("provider schema index path must be a non-empty string")
    pure = PurePosixPath(raw_path)
    if (
        raw_path != pure.as_posix()
        or "\\" in raw_path
        or ":" in raw_path
        or pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ProviderSchemaError(f"provider schema index path is not a safe package-relative path: {raw_path}")

    package_root = index_path.parent.parent.resolve()
    candidate = package_root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProviderSchemaError(f"provider schema artifact is missing: {raw_path}") from exc
    try:
        resolved.relative_to(package_root)
    except ValueError as exc:
        raise ProviderSchemaError(f"provider schema artifact escapes its package: {raw_path}") from exc
    if not resolved.is_file() or resolved == index_path.resolve():
        raise ProviderSchemaError(f"provider schema index path is not a schema artifact: {raw_path}")
    return resolved


def _validate_schema_document(value: Mapping[str, Any], schema_id: str, path: Path) -> None:
    unknown = set(value) - _SCHEMA_DOCUMENT_FIELDS
    if unknown:
        raise ProviderSchemaError(f"provider schema has unknown fields at {path}: {','.join(sorted(unknown))}")
    required_fields = {"$id", "$schema", "type", "additionalProperties", "properties", "required", "$defs"}
    missing = required_fields - set(value)
    if missing:
        raise ProviderSchemaError(f"provider schema is missing fields at {path}: {','.join(sorted(missing))}")
    if type(value.get("$id")) is not str or value.get("$id") != schema_id:
        raise ProviderSchemaError(f"provider schema ID mismatch: {path}")
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ProviderSchemaError(f"provider schema is not Draft 2020-12: {path}")
    if value.get("type") != "object" or value.get("additionalProperties") is not False:
        raise ProviderSchemaError(f"provider schema is not a closed object: {path}")
    if not isinstance(value.get("properties"), dict) or not isinstance(value.get("required"), list) or not isinstance(value.get("$defs"), dict):
        raise ProviderSchemaError(f"provider schema has invalid structural field types: {path}")
    if "oneOf" in value and not isinstance(value["oneOf"], list):
        raise ProviderSchemaError(f"provider schema oneOf must be an array: {path}")
    if Draft202012Validator is not None:
        try:
            Draft202012Validator.check_schema(dict(value))
        except Exception as exc:
            raise ProviderSchemaError(f"provider schema is not a valid Draft 2020-12 schema: {path}") from exc


def _read_verified_index(index_path: Path | None = None) -> dict[str, dict[str, Any]]:
    index_path = Path(index_path) if index_path is not None else SCHEMA_INDEX_PATH
    index = _read_json_object(index_path, label="provider schema index")
    if set(index) != _INDEX_FIELDS:
        unknown = sorted(set(index) - _INDEX_FIELDS)
        missing = sorted(_INDEX_FIELDS - set(index))
        detail = []
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        if missing:
            detail.append("missing=" + ",".join(missing))
        raise ProviderSchemaError("provider schema index fields are not exact: " + ";".join(detail))
    if type(index["schema"]) is not str or index["schema"] != "provider-schema-index/v1":
        raise ProviderSchemaError("provider schema index schema version is invalid")
    if type(index["plugin_id"]) is not str or index["plugin_id"] != PLUGIN_ID:
        raise ProviderSchemaError("provider schema index plugin owner is invalid")
    if type(index["capability_id"]) is not str or index["capability_id"] != CAPABILITY_ID:
        raise ProviderSchemaError("provider schema index capability owner is invalid")
    raw_entries = index["schemas"]
    if type(raw_entries) is not list or len(raw_entries) != len(EXPECTED_SCHEMA_IDS):
        raise ProviderSchemaError("provider schema index schemas must contain exactly two entries")

    verified: dict[str, dict[str, Any]] = {}
    path_keys: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _INDEX_ENTRY_FIELDS:
            raise ProviderSchemaError("provider schema index entry fields are not exact")
        schema_id = raw_entry["schema_id"]
        path_name = raw_entry["path"]
        declared_hash = raw_entry["sha256"]
        if type(schema_id) is not str or not schema_id:
            raise ProviderSchemaError("provider schema index schema_id must be a non-empty string")
        if schema_id not in EXPECTED_SCHEMA_IDS:
            raise ProviderSchemaError(f"provider schema index contains an unknown schema ID: {schema_id}")
        if schema_id in verified:
            raise ProviderSchemaError(f"provider schema index contains a duplicate schema ID: {schema_id}")
        if type(declared_hash) is not str or _HEX64.fullmatch(declared_hash) is None:
            raise ProviderSchemaError(f"provider schema index digest is not lowercase SHA-256: {schema_id}")
        artifact = _artifact_path(index_path, path_name)
        path_key = str(artifact).casefold()
        if path_key in path_keys:
            raise ProviderSchemaError(f"provider schema index maps multiple IDs to one artifact: {path_name}")
        path_keys.add(path_key)
        try:
            artifact_bytes = artifact.read_bytes()
        except OSError as exc:
            raise ProviderSchemaError(f"provider schema artifact cannot be read: {artifact}") from exc
        actual_hash = hashlib.sha256(artifact_bytes).hexdigest()
        if actual_hash != declared_hash:
            raise ProviderSchemaError(f"provider schema artifact digest mismatch: {schema_id}")
        artifact_value = _read_json_object(artifact, label="provider schema artifact")
        _validate_schema_document(artifact_value, schema_id, artifact)
        verified[schema_id] = {
            "schema_id": schema_id,
            "path": path_name,
            "sha256": declared_hash,
            "artifact_path": artifact,
            "artifact_bytes": artifact_bytes,
            "artifact_value": artifact_value,
        }

    if set(verified) != set(EXPECTED_SCHEMA_IDS):
        missing = sorted(set(EXPECTED_SCHEMA_IDS) - set(verified))
        raise ProviderSchemaError("provider schema index is missing schema IDs: " + ",".join(missing))
    return verified


def load_schema_index(index_path: Path | None = None) -> dict[str, dict[str, str]]:
    """Load and verify every entry in the package schema index."""

    verified = _read_verified_index(index_path)
    return {schema_id: {"path": entry["path"], "sha256": entry["sha256"]} for schema_id, entry in verified.items()}


def schema_path(schema_id: str) -> Path:
    if type(schema_id) is not str:
        raise ProviderSchemaError("provider schema ID must be a string")
    try:
        entry = _read_verified_index()[schema_id]
    except KeyError as exc:
        raise ProviderSchemaError(f"unknown provider schema: {schema_id}") from exc
    return entry["artifact_path"]


def load_schema(schema_id: str) -> dict[str, Any]:
    if type(schema_id) is not str:
        raise ProviderSchemaError("provider schema ID must be a string")
    try:
        entry = _read_verified_index()[schema_id]
    except KeyError as exc:
        raise ProviderSchemaError(f"unknown provider schema: {schema_id}") from exc
    return dict(entry["artifact_value"])


def validate_schema(schema_id: str, value: Any) -> None:
    if Draft202012Validator is None:
        raise ProviderSchemaError("jsonschema runtime is unavailable; provider validation is fail-closed") from _JSONSCHEMA_IMPORT_ERROR
    schema = load_schema(schema_id)
    try:
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: (tuple(item.absolute_path), item.message))
    except Exception as exc:
        raise ProviderSchemaError(f"provider schema validation could not be performed: {schema_id}") from exc
    if errors:
        error = errors[0]
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        raise ProviderSchemaError(f"{schema_id}{path}: {error.message}")


def schema_bytes(schema_id: str) -> bytes:
    if type(schema_id) is not str:
        raise ProviderSchemaError("provider schema ID must be a string")
    try:
        entry = _read_verified_index()[schema_id]
    except KeyError as exc:
        raise ProviderSchemaError(f"unknown provider schema: {schema_id}") from exc
    return bytes(entry["artifact_bytes"])


def schema_index() -> Mapping[str, str]:
    """Return the verified descriptor-ID to package-relative artifact mapping."""

    return {schema_id: entry["path"] for schema_id, entry in _read_verified_index().items()}


def validate_descriptor_schema_references(descriptor: Mapping[str, Any]) -> None:
    """Require both descriptor schema references to resolve uniquely in the index."""

    if not isinstance(descriptor, Mapping):
        raise ProviderSchemaError("provider descriptor must be an object")
    references = (descriptor.get("input_schema"), descriptor.get("output_schema"))
    if any(type(reference) is not str or not reference for reference in references):
        raise ProviderSchemaError("provider descriptor schema references must be non-empty strings")
    if references[0] == references[1]:
        raise ProviderSchemaError("provider descriptor input/output schema IDs must be unique")
    provider = descriptor.get("provider")
    if not isinstance(provider, Mapping) or provider.get("plugin_id") != PLUGIN_ID or descriptor.get("capability_id") != CAPABILITY_ID:
        raise ProviderSchemaError("provider descriptor owner/capability does not match the schema index")
    index = load_schema_index()
    for reference in references:
        if reference not in index:
            raise ProviderSchemaError(f"provider descriptor schema is not indexed: {reference}")
    if len({index[reference]["path"] for reference in references}) != len(references):
        raise ProviderSchemaError("provider descriptor schema references do not resolve uniquely")
