"""Closed local contract helpers backed by the public PlotPilot SDK."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

PLUGIN_ID = "com.plotpilot.novelagent.source-import"
VERSION = "0.1.0"
INSPECT_CAPABILITY = "source.import.inspect/v1"
PARSE_CAPABILITY = "source.import.parse/v1"
REQUEST_SCHEMAS = {
    INSPECT_CAPABILITY: "source.import.inspect-request/v1",
    PARSE_CAPABILITY: "source.import.parse-request/v1",
}
_COMMON_REQUEST_FIELDS = frozenset({
    "schema", "capability_id", "worker_run_id", "job_id", "step_id", "attempt_id",
    "lease_epoch", "run_snapshot_hash", "source_asset_id", "source_asset_hash",
    "source_kind", "source_name", "encoding", "page_size", "checkpoint_id",
    "checkpoint_ids", "resume_checkpoint_asset_id", "resume_checkpoint_asset_hash",
    "resume_state_asset_id", "resume_state_asset_hash", "provenance_receipt_id",
    "created_at",
})
_REQUEST_FIELDS = {
    INSPECT_CAPABILITY: _COMMON_REQUEST_FIELDS,
    PARSE_CAPABILITY: _COMMON_REQUEST_FIELDS | {
        "target", "base", "structure_target", "structure_base",
    },
}
RAW_RECEIPT_SCHEMA = "source-import-raw-receipt/v1"
DECODER_RECEIPT_SCHEMA = "source-import-decoder-receipt/v1"
PROVISIONAL_RECEIPT_SCHEMA = "source-import-provisional-receipt/v1"
CANONICAL_RECEIPT_SCHEMA = "source-import-canonical-receipt/v1"
CHECKPOINT_STATE_SCHEMA = "source-import-checkpoint-state/v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_TIMESTAMP = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.(?!000)[0-9]{3}Z)$")


class ContractError(ValueError):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    try:
        from plotpilot_plugin_sdk.canonical import canonical_bytes as sdk_canonical_bytes
    except ImportError as exc:
        raise ContractError("public PlotPilot SDK is unavailable") from exc
    return bytes(sdk_canonical_bytes(value))


def hash_jcs(prefix: str, value: Any) -> str:
    try:
        from plotpilot_plugin_sdk.canonical import hash_jcs as sdk_hash_jcs
    except ImportError as exc:
        raise ContractError("public PlotPilot SDK is unavailable") from exc
    return str(sdk_hash_jcs(prefix, value))


def hash_without_field(value: Mapping[str, Any], field: str, prefix: str) -> str:
    body = deepcopy(dict(value))
    body.pop(field, None)
    return hash_jcs(prefix, body)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ContractError(f"duplicate JSON object key: {key}")
        output[key] = value
    return output


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_strict_object_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"strict package JSON read failed: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"package JSON must be an object: {path}")
    return value


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError(f"{label} is not a valid Core identity")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ContractError(f"{label} is not a lowercase SHA-256")
    return value


def validate_source_name(source_name: str | None) -> str | None:
    if source_name is None:
        return None
    if not isinstance(source_name, str) or not source_name or len(source_name) > 240:
        raise ContractError("source_name must be a short basename")
    if "\x00" in source_name or "/" in source_name or "\\" in source_name or source_name in {".", ".."}:
        raise ContractError("source_name must not be a host path")
    if source_name[-1] in {" ", "."}:
        raise ContractError("source_name has a trailing dot/space")
    return source_name


def validate_request_common(request: Mapping[str, Any], *, capability: str) -> None:
    if not isinstance(request, Mapping):
        raise ContractError("request must be an object")
    required = {
        "schema", "worker_run_id", "job_id", "step_id", "attempt_id",
        "lease_epoch", "run_snapshot_hash", "source_asset_id",
        "source_asset_hash", "source_kind", "provenance_receipt_id",
    }
    missing = required - set(request)
    if missing:
        raise ContractError(f"request is missing fields: {sorted(missing)}")
    try:
        expected_schema = REQUEST_SCHEMAS[capability]
        allowed_fields = _REQUEST_FIELDS[capability]
    except KeyError as exc:
        raise ContractError("request capability is not declared by this plugin") from exc
    if any(not isinstance(field, str) for field in request):
        raise ContractError("request field names must be strings")
    unexpected = set(request) - allowed_fields
    if unexpected:
        raise ContractError(f"request contains undeclared fields: {sorted(unexpected)}")
    if "capability_id" in request and request["capability_id"] != capability:
        raise ContractError("request capability_id does not match descriptor")
    if request["schema"] != expected_schema:
        raise ContractError("request schema does not match capability")
    for field in ("worker_run_id", "job_id", "step_id", "attempt_id", "source_asset_id", "provenance_receipt_id"):
        _id(request[field], field)
    if isinstance(request["lease_epoch"], bool) or not isinstance(request["lease_epoch"], int) or request["lease_epoch"] < 1:
        raise ContractError("lease_epoch must be a positive integer")
    for field in ("run_snapshot_hash", "source_asset_hash"):
        _hash(request[field], field)
    if request["source_kind"] not in {"txt", "epub", "paste"}:
        raise ContractError("source_kind must be txt, epub, or paste")
    validate_source_name(request.get("source_name"))
    if "created_at" in request and (not isinstance(request["created_at"], str) or not _TIMESTAMP.fullmatch(request["created_at"])):
        raise ContractError("created_at is not a contract timestamp")
    forbidden = {"source_bytes", "path", "file_path", "url", "clipboard", "raw_text"}
    if forbidden.intersection(request):
        raise ContractError("request may contain only Core Asset identity, never host/path/raw input")
    if "page_size" in request:
        if (
            isinstance(request["page_size"], bool)
            or not isinstance(request["page_size"], int)
            or not 1 <= request["page_size"] <= 1048576
        ):
            raise ContractError("page_size must be an integer from 1 through 1048576")
    if "encoding" in request and (
        not isinstance(request["encoding"], str)
        or not 1 <= len(request["encoding"]) <= 32
    ):
        raise ContractError("encoding must be a string from 1 through 32 characters")
    for field in (
        "checkpoint_id", "resume_checkpoint_asset_id", "resume_state_asset_id",
    ):
        if field in request:
            _id(request[field], field)
    for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"):
        if field in request:
            _hash(request[field], field)
    if "checkpoint_ids" in request:
        checkpoint_ids = request["checkpoint_ids"]
        if not isinstance(checkpoint_ids, list):
            raise ContractError("checkpoint_ids must be an array")
        validated_ids = [_id(value, "checkpoint_ids item") for value in checkpoint_ids]
        if len(set(validated_ids)) != len(validated_ids):
            raise ContractError("checkpoint_ids must be unique")
    if capability == PARSE_CAPABILITY:
        for field in ("target",):
            target = request.get(field)
            if not isinstance(target, Mapping) or set(target) != {"workspace_id", "entity_kind", "entity_id"}:
                raise ContractError("parse request target must be a closed Core target")
            _id(target["workspace_id"], "target.workspace_id")
            _id(target["entity_id"], "target.entity_id")
            if target["entity_kind"] not in {"document", "node_structure"}:
                raise ContractError("target.entity_kind is invalid")
        base = request.get("base")
        if not isinstance(base, Mapping) or set(base) != {"revision_id", "content_hash"}:
            raise ContractError("parse request base must be the Core-issued closed base")
        _id(base["revision_id"], "base.revision_id")
        _hash(base["content_hash"], "base.content_hash")
        structure_target = request.get("structure_target")
        if structure_target is not None:
            if not isinstance(structure_target, Mapping) or set(structure_target) != {"workspace_id", "entity_kind", "entity_id"}:
                raise ContractError("structure_target must be a closed Core target")
            _id(structure_target["workspace_id"], "structure_target.workspace_id")
            _id(structure_target["entity_id"], "structure_target.entity_id")
            if structure_target["entity_kind"] != "node_structure":
                raise ContractError("structure_target must be node_structure")
        structure_base = request.get("structure_base")
        if structure_base is not None:
            if not isinstance(structure_base, Mapping) or set(structure_base) != {"revision_id", "content_hash"}:
                raise ContractError("structure_base must be a closed Core base")
            _id(structure_base["revision_id"], "structure_base.revision_id")
            _hash(structure_base["content_hash"], "structure_base.content_hash")


def build_raw_receipt(
    *,
    source_asset_id: str | None,
    source_asset_hash: str,
    source_kind: str,
    source_name: str | None,
    size_bytes: int,
) -> dict[str, Any]:
    body = {
        "schema": RAW_RECEIPT_SCHEMA,
        "source_asset_id": source_asset_id,
        "source_asset_hash": _hash(source_asset_hash, "source_asset_hash"),
        "source_kind": source_kind,
        "source_name": validate_source_name(source_name),
        "size_bytes": size_bytes,
        "immutable": True,
    }
    body["receipt_hash"] = hash_without_field(body, "receipt_hash", RAW_RECEIPT_SCHEMA)
    return body


def build_decoder_receipt(
    *,
    raw_receipt: Mapping[str, Any],
    encoding: str,
    selection_source: str,
    had_bom: bool,
    replacement_count: int,
    decoded_text_hash: str,
) -> dict[str, Any]:
    body = {
        "schema": DECODER_RECEIPT_SCHEMA,
        "raw_asset_id": raw_receipt["source_asset_id"],
        "raw_asset_hash": raw_receipt["source_asset_hash"],
        "decoder_policy": "txt-lossless" if raw_receipt["source_kind"] in {"txt", "paste"} else "epub-xml-lossless",
        "decoder_policy_version": "v1",
        "encoding": encoding,
        "selection_source": selection_source,
        "had_bom": bool(had_bom),
        "replacement_count": replacement_count,
        "lossless": replacement_count == 0,
        "decoded_text_hash": _hash(decoded_text_hash, "decoded_text_hash"),
    }
    body["receipt_hash"] = hash_without_field(body, "receipt_hash", DECODER_RECEIPT_SCHEMA)
    return body


def build_provisional_receipt(
    *,
    raw_receipt: Mapping[str, Any],
    decoder_receipt: Mapping[str, Any],
    provisional_asset_id: str | None,
    provisional_text_hash: str,
    provisional_byte_length: int,
    structure_evidence_hash: str,
    source_kind: str,
) -> dict[str, Any]:
    body = {
        "schema": PROVISIONAL_RECEIPT_SCHEMA,
        "raw_receipt_hash": raw_receipt["receipt_hash"],
        "decoder_receipt_hash": decoder_receipt["receipt_hash"],
        "provisional_asset_id": provisional_asset_id,
        "provisional_text_hash": _hash(provisional_text_hash, "provisional_text_hash"),
        "provisional_byte_length": provisional_byte_length,
        "structure_evidence_hash": _hash(structure_evidence_hash, "structure_evidence_hash"),
        "source_kind": source_kind,
    }
    body["receipt_hash"] = hash_without_field(body, "receipt_hash", PROVISIONAL_RECEIPT_SCHEMA)
    return body


def build_canonical_receipt(
    *,
    raw_receipt: Mapping[str, Any],
    decoder_receipt: Mapping[str, Any],
    provisional_receipt: Mapping[str, Any],
    canonical_asset_id: str | None,
    canonical_text_hash: str,
    canonical_byte_length: int,
    structure_evidence_hash: str,
) -> dict[str, Any]:
    body = {
        "schema": CANONICAL_RECEIPT_SCHEMA,
        "raw_receipt_hash": raw_receipt["receipt_hash"],
        "decoder_receipt_hash": decoder_receipt["receipt_hash"],
        "provisional_receipt_hash": provisional_receipt["receipt_hash"],
        "canonical_asset_id": canonical_asset_id,
        "canonical_text_hash": _hash(canonical_text_hash, "canonical_text_hash"),
        "canonical_byte_length": canonical_byte_length,
        "structure_evidence_hash": _hash(structure_evidence_hash, "structure_evidence_hash"),
        "coordinate_system": "python-unicode-codepoint/v1",
    }
    body["receipt_hash"] = hash_without_field(body, "receipt_hash", CANONICAL_RECEIPT_SCHEMA)
    return body


def verify_self_hashed(value: Mapping[str, Any], *, field: str, prefix: str) -> None:
    expected = hash_without_field(value, field, prefix)
    if value.get(field) != expected:
        raise ContractError(f"{prefix} self hash mismatch")


def build_provenance_receipt(
    request: Mapping[str, Any],
    *,
    capability_id: str,
    release_id: str,
    package_hash: str,
    bundle_id: str | None,
    bundle_hash: str | None,
    staged_items: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "provenance-receipt/v1",
        "receipt_id": request["provenance_receipt_id"],
        "plugin_id": PLUGIN_ID,
        "release_id": _hash(release_id, "release_id"),
        "package_hash": _hash(package_hash, "package_hash"),
        "capability_id": capability_id,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "bundle_id": bundle_id,
        "bundle_hash": bundle_hash,
        "parent_receipt_ids": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": list(staged_items or []),
        "created_at": request.get("created_at", "2026-08-28T00:00:00Z"),
    }
    if any(not isinstance(item_id, str) or not _ID.fullmatch(item_id) for item_id in body["staged_items"]):
        raise ContractError("staged_items must contain Core item IDs")
    if len(set(body["staged_items"])) != len(body["staged_items"]):
        raise ContractError("staged_items must be unique")
    body["receipt_hash"] = hash_without_field(body, "receipt_hash", "provenance-receipt/v1")
    try:
        from plotpilot_plugin_sdk.verifier import verify_provenance_receipt
        verify_provenance_receipt(body)
    except ImportError as exc:
        raise ContractError("public PlotPilot SDK is unavailable") from exc
    return body


def build_checkpoint(
    request: Mapping[str, Any],
    *,
    checkpoint_id: str,
    checkpoint_seq: int,
    completed_units: int,
    total_units: int | None,
    state_asset_id: str | None,
    unit_set_hash: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "checkpoint/v1",
        "checkpoint_id": _id(checkpoint_id, "checkpoint_id"),
        "checkpoint_seq": checkpoint_seq,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "source_attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "replay_policy": "checkpoint_resume",
        "completed_units": completed_units,
        "total_units": total_units,
        "unit_set_hash": unit_set_hash,
        "state_asset_id": state_asset_id,
        "created_at": request.get("created_at", "2026-08-28T00:00:00Z"),
    }
    body["checkpoint_hash"] = hash_without_field(body, "checkpoint_hash", "checkpoint/v1")
    try:
        from plotpilot_plugin_sdk.verifier import verify_checkpoint
        verify_checkpoint(body, expected_snapshot_hash=request["run_snapshot_hash"])
    except ImportError as exc:
        raise ContractError("public PlotPilot SDK is unavailable") from exc
    return body


def verify_bundle(bundle: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    try:
        from plotpilot_plugin_sdk.verifier import verify_result_bundle
    except ImportError as exc:
        raise ContractError("public PlotPilot SDK is unavailable") from exc
    verify_result_bundle(
        bundle,
        snapshot_workspace_id=(
            request.get("target", {}).get("workspace_id")
            if isinstance(request.get("target"), Mapping)
            else None
        ),
        snapshot_hash_value=request["run_snapshot_hash"],
    )
