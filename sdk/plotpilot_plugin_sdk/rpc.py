"""RPC method matrix, profiles and durable operation-key semantics."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .canonical import canonical_bytes
from .errors import ContractError, ErrorCode
from .framing import decode_frame, encode_frame
from ._workspace import WORKSPACE_ROOT


ROOT = WORKSPACE_ROOT
MATRIX_PATH = ROOT / "contracts" / "json-schema" / "rpc-method-matrix.v1.json"
METHOD_MATRIX: dict[str, Any] = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
ERROR_CODES: dict[int, str] = {int(k): v for k, v in METHOD_MATRIX["error_codes"].items()}
WORKER_METHODS: tuple[str, ...] = tuple(METHOD_MATRIX["worker_methods"])
HOST_METHODS: tuple[str, ...] = tuple(METHOD_MATRIX["host_methods"])
ALL_METHODS: tuple[str, ...] = WORKER_METHODS + HOST_METHODS


def _uuid(value: str | None = None) -> str:
    return value or str(uuid.uuid4())


def build_meta(
    context: str,
    *,
    generation_id: str,
    plugin_release_id: str,
    deadline_at: str,
    operation_id: str | None = None,
    install_operation_id: str | None = None,
    install_lease_epoch: int | None = None,
    job_id: str | None = None,
    step_id: str | None = None,
    attempt_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "protocol_version": "1",
        "generation_id": generation_id,
        "plugin_release_id": plugin_release_id,
        "deadline_at": deadline_at,
        "context": context,
        "operation_id": operation_id or _uuid(),
    }
    if context == "install":
        value.update({"install_operation_id": install_operation_id or _uuid(), "install_lease_epoch": install_lease_epoch or 1})
    elif context == "attempt":
        if not all((job_id, step_id, attempt_id)) or lease_epoch is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "attempt meta requires job/step/attempt and lease epoch")
        value.update({"job_id": job_id, "step_id": step_id, "attempt_id": attempt_id, "lease_epoch": lease_epoch})
    elif context != "control":
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"unknown RPC context: {context}")
    return value


def build_request(method: str, params: Mapping[str, Any], meta: Mapping[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
    if method not in METHOD_MATRIX["methods"]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"unknown RPC method: {method}")
    if method == "runtime.heartbeat":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "runtime.heartbeat is the only notification, not a request")
    return {"jsonrpc": "2.0", "id": request_id or _uuid(), "method": method, "meta": dict(meta), "params": dict(params)}


def build_notification(params: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "runtime.heartbeat", "meta": dict(meta), "params": dict(params)}


@dataclass
class OperationLedger:
    """In-memory test double for Core's durable operation ledger.

    Production implementations persist the same tuple and response in one
    transaction.  This fake deliberately returns bytes to make ACK-loss
    equivalence observable in tests.
    """

    _responses: dict[tuple[str, str, str], tuple[str, bytes]] = field(default_factory=dict)

    def apply_frame(
        self,
        context_identity: str,
        method: str,
        operation_key: str,
        payload: Mapping[str, Any],
        action: Callable[[], Mapping[str, Any]],
        *,
        response_id: str | None = None,
    ) -> bytes:
        """Apply once and return the exact first response frame on retries."""
        key = (context_identity, method, operation_key)
        payload_hash = hashlib.sha256(canonical_bytes(dict(payload))).hexdigest()
        previous = self._responses.get(key)
        if previous is not None:
            old_hash, response_bytes = previous
            if old_hash != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
            return response_bytes
        response = {"jsonrpc": "2.0", "id": response_id or _uuid(), "result": dict(action())}
        response_bytes = encode_frame(response)
        self._responses[key] = (payload_hash, response_bytes)
        return response_bytes

    def apply(self, context_identity: str, method: str, operation_key: str, payload: Mapping[str, Any], action: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        frame = self.apply_frame(context_identity, method, operation_key, payload, action)
        value = decode_frame(frame)
        result = value.get("result")
        if not isinstance(result, dict):
            raise ContractError(ErrorCode.ASSET_ERROR, "ledger response result is not an object")
        return result


@dataclass
class ChunkUploadLedger:
    """Deterministic chunk upload fixture covering ACK-loss/status recovery."""

    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    operations: dict[tuple[str, str], tuple[str, bytes]] = field(default_factory=dict)

    def create(
        self,
        *,
        operation_key: str,
        upload_id: str,
        offset: int,
        total_size: int,
        expected_hash: str,
        chunk_hash: str,
        base64_chunk: str,
        final: bool,
    ) -> dict[str, Any]:
        payload = {
            "upload_id": upload_id,
            "offset": offset,
            "total_size": total_size,
            "expected_hash": expected_hash,
            "chunk_hash": chunk_hash,
            "base64_chunk": base64_chunk,
            "final": final,
        }
        key = (upload_id, operation_key)
        payload_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
        previous = self.operations.get(key)
        if previous is not None:
            if previous[0] != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "upload operation key reused with a different payload")
            return json.loads(previous[1].decode("utf-8"))
        try:
            chunk = base64.b64decode(base64_chunk, validate=True)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "upload chunk is not valid base64") from exc
        if hashlib.sha256(chunk).hexdigest() != chunk_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "upload chunk hash mismatch")
        if total_size < 0 or offset < 0 or offset + len(chunk) > total_size:
            raise ContractError(ErrorCode.ASSET_ERROR, "upload chunk is outside total size")
        state = self.uploads.setdefault(upload_id, {"total_size": total_size, "expected_hash": expected_hash, "data": b"", "completed": False, "asset_id": None})
        if state["total_size"] != total_size or state["expected_hash"] != expected_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "upload identity changed")
        if state["completed"]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "completed upload cannot be mutated")
        if offset != len(state["data"]):
            raise ContractError(ErrorCode.ASSET_ERROR, "upload offset is not the next acknowledged offset")
        state["data"] += chunk
        if final:
            if len(state["data"]) != total_size or hashlib.sha256(state["data"]).hexdigest() != expected_hash:
                raise ContractError(ErrorCode.ASSET_ERROR, "final upload hash or size mismatch")
            state["completed"] = True
            state["asset_id"] = f"asset-upload-{upload_id}"
        result = {"upload_id": upload_id, "accepted_bytes": len(state["data"]), "completed": state["completed"], "asset_id": state["asset_id"]}
        self.operations[key] = (payload_hash, json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return result

    def status(self, upload_id: str, expected_hash: str) -> dict[str, Any]:
        state = self.uploads.get(upload_id)
        if state is None or state["expected_hash"] != expected_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "unknown upload or expected hash")
        return {"accepted_bytes": len(state["data"]), "completed": state["completed"], "asset_id": state["asset_id"]}


def enforce_lease(*, expected_epoch: int, actual_epoch: int, context: str) -> None:
    if expected_epoch != actual_epoch:
        raise ContractError(ErrorCode.STALE_LEASE, f"{context} lease epoch is stale")


__all__ = [
    "ERROR_CODES",
    "METHOD_MATRIX",
    "OperationLedger",
    "ChunkUploadLedger",
    "build_meta",
    "build_notification",
    "build_request",
    "decode_frame",
    "encode_frame",
    "enforce_lease",
]
