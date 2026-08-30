"""Host-bound runtime for the four NAP-01 source-structure capabilities."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
import threading
from typing import Any, Mapping, Protocol

from .contract import (
    apply_structure_operations,
    build_rebind_report,
    find_text_matches,
    hash_json,
    rebind_evidence,
    sha256_text,
    validate_nodes,
    validate_rebind_report,
)
from .package_identity import load_runtime_identity

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes_sdk
    from plotpilot_plugin_sdk.verifier import (
        validate_rpc_result as _validate_rpc_result_sdk,
        verify_checkpoint as _verify_checkpoint_sdk,
        verify_provenance_receipt as _verify_provenance_receipt_sdk,
        verify_result_bundle as _verify_result_bundle_sdk,
    )
except ImportError as exc:  # pragma: no cover - exercised by isolated install tests
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _canonical_bytes_sdk = None
    _validate_rpc_result_sdk = None
    _verify_checkpoint_sdk = None
    _verify_provenance_receipt_sdk = None
    _verify_result_bundle_sdk = None
else:
    _SDK_IMPORT_ERROR = None


PLUGIN_ID = "com.plotpilot.novelagent.source-structure"
VERSION = "0.1.0"
CAPABILITY_REVISE = "source.structure.revise/v1"
CAPABILITY_SEARCH = "source.evidence.search/v1"
CAPABILITY_REBIND_INSPECT = "source.evidence.rebind.inspect/v1"
CAPABILITY_REBIND_PROPOSE = "source.evidence.rebind.propose/v1"
CAPABILITIES = (
    CAPABILITY_REVISE,
    CAPABILITY_SEARCH,
    CAPABILITY_REBIND_INSPECT,
    CAPABILITY_REBIND_PROPOSE,
)
NEEDS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.candidate.stage/v1",
)
_ZERO_HASH = "0" * 64
_PAGE_SIZE = 8_388_608
_MAX_PAGES = 4096
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIME = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)
_COMMON_FIELDS = frozenset(
    {
        "schema",
        "capability_id",
        "operation_key",
        "operation",
        "job_id",
        "step_id",
        "attempt_id",
        "worker_run_id",
        "lease_epoch",
        "checkpoint_ids",
        "provenance_receipt_id",
        "created_at",
        "total_units",
        "run_snapshot_hash",
        "workspace_id",
        "document_id",
    }
)
_REVISE_FIELDS = frozenset(
    {"revision_id", "canonical_asset_id", "canonical_text_hash", "nodes", "operations"}
)
_SEARCH_FIELDS = frozenset(
    {
        "revision_id",
        "canonical_asset_id",
        "canonical_text_hash",
        "nodes",
        "query",
        "case_sensitive",
        "max_matches",
    }
)
_REBIND_FIELDS = frozenset(
    {
        "source_revision_id",
        "source_canonical_asset_id",
        "source_canonical_text_hash",
        "target_revision_id",
        "target_canonical_asset_id",
        "target_canonical_text_hash",
        "source_nodes",
        "target_nodes",
        "evidence_items",
        "selected_evidence_ids",
        "known_parent_candidate_ids",
    }
)
_RESUME_FIELDS = frozenset(
    {
        "resume_checkpoint_asset_id",
        "resume_checkpoint_asset_hash",
        "resume_state_asset_id",
        "resume_state_asset_hash",
    }
)
_SCHEMAS = {
    CAPABILITY_REVISE: "source.structure.revise-request/v1",
    CAPABILITY_SEARCH: "source.evidence.search-request/v1",
    CAPABILITY_REBIND_INSPECT: "source.evidence.rebind.inspect-request/v1",
    CAPABILITY_REBIND_PROPOSE: "source.evidence.rebind.propose-request/v1",
}
_OPERATIONS = {
    CAPABILITY_REVISE: frozenset({"run", "validate"}),
    CAPABILITY_SEARCH: frozenset({"run"}),
    CAPABILITY_REBIND_INSPECT: frozenset({"run", "resume", "cancel"}),
    CAPABILITY_REBIND_PROPOSE: frozenset({"run", "validate"}),
}
_RAW_AUTHORITY_FIELDS = frozenset(
    {
        "canonical_text",
        "source_canonical_text",
        "target_canonical_text",
        "canonical_path",
        "source_canonical_path",
        "target_canonical_path",
        "path",
        "file_path",
        "url",
    }
)


class StructureWorkerError(RuntimeError):
    """Fail-closed worker or Host boundary error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TerminalContractError(StructureWorkerError):
    """A terminal Host acknowledgement failed and must never be swallowed."""


class HostPort(Protocol):
    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        ...


@dataclass
class _RunContext:
    request_hash: str
    binding_hash: str
    checkpoint_ids: tuple[str, ...]
    last_job_event_seq: int = 0
    last_checkpoint_seq: int = 0
    last_local_seq: int = 0
    stage_response: dict[str, object] | None = None


@dataclass
class _ActiveRun:
    worker_run_id: str
    binding_hash: str
    cancelled: threading.Event


class _OperationDispatcher:
    """Module-owned max_concurrency=1 dispatcher for run/resume/cancel."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: _ActiveRun | None = None

    def begin(self, worker_run_id: str, binding_hash: str) -> _ActiveRun:
        with self._lock:
            if self._active is not None:
                raise StructureWorkerError(
                    "WORKER_BUSY",
                    "source-structure already has an active operation",
                    retryable=True,
                )
            active = _ActiveRun(worker_run_id, binding_hash, threading.Event())
            self._active = active
            return active

    def cancel(self, worker_run_id: str, binding_hash: str) -> bool:
        with self._lock:
            active = self._active
            if (
                active is None
                or active.worker_run_id != worker_run_id
                or active.binding_hash != binding_hash
            ):
                return False
            active.cancelled.set()
            return True

    def finish(self, active: _ActiveRun) -> None:
        with self._lock:
            if self._active is active:
                self._active = None


_DISPATCHER = _OperationDispatcher()


def _identity() -> tuple[str, str]:
    try:
        value = load_runtime_identity()
    except Exception as exc:
        raise StructureWorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc
    return str(value["package_hash"]), str(value["release_id"])


PACKAGE_HASH, RELEASE_ID = _identity()


def _require_sdk() -> None:
    if (
        _SDK_IMPORT_ERROR is not None
        or _canonical_bytes_sdk is None
        or _validate_rpc_result_sdk is None
        or _verify_checkpoint_sdk is None
        or _verify_provenance_receipt_sdk is None
        or _verify_result_bundle_sdk is None
    ):
        raise StructureWorkerError(
            "SDK_UNAVAILABLE",
            "public PlotPilot SDK is unavailable; source-structure is fail-closed",
        ) from _SDK_IMPORT_ERROR


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise StructureWorkerError(
            "INPUT_INVALID", f"{label} must be a Core identifier"
        )
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise StructureWorkerError(
            "INPUT_INVALID", f"{label} must be lowercase SHA-256"
        )
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StructureWorkerError(
            "INPUT_INVALID", f"{label} must be an integer >= {minimum}"
        )
    return value


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    _require_sdk()
    assert _canonical_bytes_sdk is not None
    try:
        return bytes(_canonical_bytes_sdk(value))
    except Exception as exc:
        raise StructureWorkerError("CANONICALIZATION_ERROR", str(exc)) from exc


def _strict_json(raw: bytes, *, code: str = "ASSET_READ_ERROR") -> Any:
    def duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON contains a duplicate object key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=duplicate,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except Exception as exc:
        raise StructureWorkerError(
            code, "Asset is not strict UTF-8 JSON"
        ) from exc


def _host_call(
    host: HostPort | None, method: str, params: Mapping[str, object]
) -> dict[str, object]:
    _require_sdk()
    if host is None or not hasattr(host, "call"):
        raise StructureWorkerError(
            "HOST_REQUIRED", "Core HostPort.call is required"
        )
    request_params = dict(params)
    try:
        response = host.call(method, request_params)
    except StructureWorkerError:
        raise
    except Exception as exc:
        raise StructureWorkerError(
            "HOST_RPC_ERROR",
            f"{method}: {type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc
    if not isinstance(response, Mapping):
        raise StructureWorkerError(
            "HOST_CONTRACT_ERROR", f"{method} returned a non-object"
        )
    assert _validate_rpc_result_sdk is not None
    try:
        _validate_rpc_result_sdk(
            method,
            dict(response),
            request={"method": method, "params": request_params},
        )
    except Exception as exc:
        raise StructureWorkerError(
            "HOST_CONTRACT_ERROR",
            f"{method} result failed the public RPC schema: {exc}",
        ) from exc
    return dict(response)


def _read_asset(
    host: HostPort | None, asset_id: str, expected_hash: str
) -> bytes:
    _id(asset_id, "asset_id")
    _hash(expected_hash, "expected Asset hash")
    offset = 0
    chunks: list[bytes] = []
    for _ in range(_MAX_PAGES):
        response = _host_call(
            host,
            "host.asset.read/v1",
            {"asset_id": asset_id, "offset": offset, "length": _PAGE_SIZE},
        )
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise StructureWorkerError(
                "ASSET_READ_ERROR", "Asset page is missing base64_chunk"
            )
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise StructureWorkerError(
                "ASSET_READ_ERROR", "Asset page base64 is invalid"
            ) from exc
        if (
            len(chunk) > _PAGE_SIZE
            or response.get("content_hash") != _hash_bytes(chunk)
        ):
            raise StructureWorkerError(
                "ASSET_READ_ERROR", "Asset page length/hash is invalid"
            )
        chunks.append(chunk)
        expected_next = offset + len(chunk)
        next_offset = response.get("next_offset")
        if next_offset is None:
            break
        if (
            isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or next_offset != expected_next
            or next_offset <= offset
        ):
            raise StructureWorkerError(
                "ASSET_READ_ERROR", "Asset next_offset is not contiguous"
            )
        offset = next_offset
    else:
        raise StructureWorkerError(
            "ASSET_READ_ERROR", "Asset exceeded the bounded page count"
        )
    data = b"".join(chunks)
    if _hash_bytes(data) != expected_hash:
        raise StructureWorkerError("ASSET_READ_ERROR", "Asset hash mismatch")
    return data


def _request_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-request/v1", dict(request))


def _binding_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        key: deepcopy(child)
        for key, child in request.items()
        if key not in _RESUME_FIELDS
    }
    value["operation"] = "run"
    return value


def _binding_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(
        capability + "-binding/v1", _binding_projection(request)
    )


def _validate_context(
    request: Mapping[str, Any], capability: str
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise StructureWorkerError(
            "INPUT_INVALID", "request must be an object"
        )
    value = dict(request)
    if any(field in value for field in _RAW_AUTHORITY_FIELDS):
        raise StructureWorkerError(
            "INPUT_INVALID",
            "raw text/path/url request fields are forbidden; use Host Asset identity",
        )
    operation = value.get("operation")
    if operation not in _OPERATIONS[capability]:
        raise StructureWorkerError(
            "INPUT_INVALID",
            f"operation {operation!r} is not declared for {capability}",
        )
    capability_fields = (
        _REVISE_FIELDS
        if capability == CAPABILITY_REVISE
        else _SEARCH_FIELDS
        if capability == CAPABILITY_SEARCH
        else _REBIND_FIELDS
    )
    allowed = set(_COMMON_FIELDS | capability_fields)
    if capability == CAPABILITY_REBIND_INSPECT and operation == "resume":
        allowed.update(_RESUME_FIELDS)
    if set(value) != allowed:
        missing = sorted(allowed - set(value))
        extra = sorted(set(value) - allowed)
        raise StructureWorkerError(
            "INPUT_INVALID",
            f"request fields are not closed: missing={missing}, extra={extra}",
        )
    if (
        value["schema"] != _SCHEMAS[capability]
        or value["capability_id"] != capability
        or value["operation_key"] != capability
    ):
        raise StructureWorkerError(
            "INPUT_INVALID",
            "request schema/capability/operation_key identity mismatch",
        )
    for field in (
        "job_id",
        "step_id",
        "attempt_id",
        "worker_run_id",
        "provenance_receipt_id",
        "workspace_id",
        "document_id",
    ):
        _id(value[field], field)
    _integer(value["lease_epoch"], "lease_epoch", 1)
    _integer(value["total_units"], "total_units", 1)
    _hash(value["run_snapshot_hash"], "run_snapshot_hash")
    if (
        not isinstance(value["created_at"], str)
        or _TIME.fullmatch(value["created_at"]) is None
    ):
        raise StructureWorkerError(
            "INPUT_INVALID",
            "created_at must be the frozen RFC3339 UTC form",
        )
    checkpoints = value["checkpoint_ids"]
    if (
        not isinstance(checkpoints, list)
        or not checkpoints
        or len(checkpoints) != len(set(checkpoints))
    ):
        raise StructureWorkerError(
            "INPUT_INVALID",
            "checkpoint_ids must be a non-empty unique array",
        )
    for index, checkpoint_id in enumerate(checkpoints):
        _id(checkpoint_id, f"checkpoint_ids[{index}]")

    if capability in {CAPABILITY_REVISE, CAPABILITY_SEARCH}:
        _id(value["revision_id"], "revision_id")
        _id(value["canonical_asset_id"], "canonical_asset_id")
        _hash(value["canonical_text_hash"], "canonical_text_hash")
        if not isinstance(value["nodes"], list):
            raise StructureWorkerError(
                "INPUT_INVALID", "nodes must be an array"
            )
        if capability == CAPABILITY_REVISE and not isinstance(
            value["operations"], list
        ):
            raise StructureWorkerError(
                "INPUT_INVALID", "operations must be an array"
            )
        if capability == CAPABILITY_SEARCH:
            if (
                not isinstance(value["query"], str)
                or not value["query"]
            ):
                raise StructureWorkerError(
                    "INPUT_INVALID", "query must be non-empty"
                )
            if not isinstance(value["case_sensitive"], bool):
                raise StructureWorkerError(
                    "INPUT_INVALID", "case_sensitive must be boolean"
                )
            _integer(value["max_matches"], "max_matches", 1)
    else:
        for field in (
            "source_revision_id",
            "source_canonical_asset_id",
            "target_revision_id",
            "target_canonical_asset_id",
        ):
            _id(value[field], field)
        for field in (
            "source_canonical_text_hash",
            "target_canonical_text_hash",
        ):
            _hash(value[field], field)
        for field in (
            "source_nodes",
            "target_nodes",
            "evidence_items",
            "selected_evidence_ids",
            "known_parent_candidate_ids",
        ):
            if not isinstance(value[field], list):
                raise StructureWorkerError(
                    "INPUT_INVALID", f"{field} must be an array"
                )
        for field in (
            "selected_evidence_ids",
            "known_parent_candidate_ids",
        ):
            if len(value[field]) != len(set(value[field])):
                raise StructureWorkerError(
                    "INPUT_INVALID", f"{field} must be unique"
                )
            for index, child in enumerate(value[field]):
                _id(child, f"{field}[{index}]")
        if operation == "resume":
            for field in (
                "resume_checkpoint_asset_id",
                "resume_state_asset_id",
            ):
                _id(value[field], field)
            for field in (
                "resume_checkpoint_asset_hash",
                "resume_state_asset_hash",
            ):
                _hash(value[field], field)
    return value


def _context(
    request: Mapping[str, Any], capability: str
) -> tuple[dict[str, Any], _RunContext]:
    value = _validate_context(request, capability)
    return value, _RunContext(
        request_hash=_request_hash(value, capability),
        binding_hash=_binding_hash(value, capability),
        checkpoint_ids=tuple(value["checkpoint_ids"]),
    )


def _load_text(
    request: Mapping[str, Any],
    host: HostPort | None,
    prefix: str = "canonical",
) -> str:
    if prefix == "canonical":
        asset_field, hash_field = (
            "canonical_asset_id",
            "canonical_text_hash",
        )
    elif prefix == "source":
        asset_field, hash_field = (
            "source_canonical_asset_id",
            "source_canonical_text_hash",
        )
    elif prefix == "target":
        asset_field, hash_field = (
            "target_canonical_asset_id",
            "target_canonical_text_hash",
        )
    else:  # pragma: no cover - internal invariant
        raise StructureWorkerError(
            "INTERNAL_ERROR", "unknown canonical Asset prefix"
        )
    asset_id = _id(request.get(asset_field), asset_field)
    expected_hash = _hash(request.get(hash_field), hash_field)
    try:
        text = _read_asset(host, asset_id, expected_hash).decode(
            "utf-8", "strict"
        )
    except UnicodeDecodeError as exc:
        raise StructureWorkerError(
            "ASSET_READ_ERROR",
            f"{asset_field} is not strict UTF-8",
        ) from exc
    if sha256_text(text) != expected_hash:
        raise StructureWorkerError(
            "ASSET_READ_ERROR",
            f"{hash_field} does not bind the canonical text",
        )
    return text


def _nodes(
    request: Mapping[str, Any], text: str, field: str
) -> list[dict[str, Any]]:
    try:
        return validate_nodes(request[field], text_length=len(text))
    except Exception as exc:
        raise StructureWorkerError(
            "INPUT_INVALID", f"{field}: {exc}"
        ) from exc


def _producer(
    request: Mapping[str, Any], capability: str, release_id: str
) -> dict[str, Any]:
    return {
        "plugin_id": PLUGIN_ID,
        "release_id": release_id,
        "capability_id": capability,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
    }


def _bundle(
    request: Mapping[str, Any],
    capability: str,
    contract_id: str,
    bundle_type: str,
    bundle_id: str,
    items: list[dict[str, Any]],
    release_id: str,
    *,
    partial: bool = False,
) -> dict[str, Any]:
    bundle = {
        "schema": "result-bundle/v1",
        "contract_id": contract_id,
        "bundle_id": bundle_id,
        "bundle_type": bundle_type,
        "producer": _producer(request, capability, release_id),
        "input_snapshot_hash": request["run_snapshot_hash"],
        "items": items,
        "warnings": [],
        "partial": partial,
        "provenance_receipt_id": request["provenance_receipt_id"],
        "skill_chain_result_refs": [],
    }
    _require_sdk()
    assert _verify_result_bundle_sdk is not None
    try:
        _verify_result_bundle_sdk(
            bundle,
            snapshot_workspace_id=(
                request.get("workspace_id")
                if contract_id == "candidate-batch/v1"
                else None
            ),
            snapshot_hash_value=request["run_snapshot_hash"],
            known_parent_ids=set(
                request.get("known_parent_candidate_ids", [])
            ),
            attempt_state="failed" if partial else None,
        )
    except Exception as exc:
        raise StructureWorkerError(
            "RESULT_CONTRACT_ERROR", str(exc)
        ) from exc
    return bundle


def _upload(
    host: HostPort | None,
    context: _RunContext,
    data: bytes,
    mime: str,
    suffix: str,
) -> str:
    expected_hash = _hash_bytes(data)
    upload_id = context.request_hash + "-upload-" + suffix
    chunks = (
        [b""]
        if not data
        else [
            data[offset : offset + _PAGE_SIZE]
            for offset in range(0, len(data), _PAGE_SIZE)
        ]
    )
    accepted = 0
    asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _host_call(
            host,
            "host.asset.create/v1",
            {
                "operation_key": context.request_hash,
                "upload_id": upload_id,
                "offset": accepted,
                "mime": mime,
                "total_size": len(data),
                "expected_hash": expected_hash,
                "chunk_hash": _hash_bytes(chunk),
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "final": final,
            },
        )
        if (
            response.get("upload_id") != upload_id
            or response.get("accepted_bytes") != accepted + len(chunk)
            or response.get("completed") is not final
        ):
            raise StructureWorkerError(
                "ASSET_CREATE_ERROR",
                "Host Asset upload acknowledgement is not contiguous",
            )
        raw_id = response.get("asset_id")
        if final:
            asset_id = _id(raw_id, "Host final asset_id")
        elif raw_id is not None:
            raise StructureWorkerError(
                "ASSET_CREATE_ERROR",
                "non-final upload returned an Asset ID",
            )
        accepted += len(chunk)
    status = _host_call(
        host,
        "host.asset.upload.status/v1",
        {"upload_id": upload_id, "expected_hash": expected_hash},
    )
    if (
        status.get("accepted_bytes") != len(data)
        or status.get("completed") is not True
        or status.get("asset_id") != asset_id
    ):
        raise StructureWorkerError(
            "ASSET_UPLOAD_ERROR",
            "Host upload status does not match completed Asset",
        )
    assert asset_id is not None
    return asset_id


def _record_event(
    context: _RunContext,
    response: Mapping[str, object],
    method: str,
) -> None:
    if response.get("accepted") is not True:
        raise StructureWorkerError(
            "HOST_REJECTED", f"Core rejected {method}"
        )
    sequence = response.get("job_event_seq")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= context.last_job_event_seq
    ):
        raise StructureWorkerError(
            "HOST_CONTRACT_ERROR",
            f"{method} job_event_seq is not strictly monotonic",
        )
    context.last_job_event_seq = sequence


def _event(
    host: HostPort | None,
    context: _RunContext,
    event_type: str,
    payload_asset_id: str | None,
    local_seq: int,
) -> None:
    if local_seq <= context.last_local_seq:
        raise StructureWorkerError(
            "HOST_CONTRACT_ERROR",
            "local event sequence is not strictly monotonic",
        )
    response = _host_call(
        host,
        "host.job.event/v1",
        {
            "operation_key": context.request_hash,
            "event_type": event_type,
            "payload_asset_id": payload_asset_id,
            "local_seq": local_seq,
        },
    )
    _record_event(context, response, "host.job.event/v1")
    context.last_local_seq = local_seq


def _stage(
    host: HostPort | None,
    request: Mapping[str, Any],
    context: _RunContext,
    bundle_asset_id: str,
    items: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    if not items:
        raise StructureWorkerError(
            "CANDIDATE_STAGE_ERROR",
            "Candidate stage requires at least one emitted Candidate item",
        )
    stage_key = context.request_hash + "-candidate-stage"
    response = _host_call(
        host,
        "host.candidate.stage/v1",
        {
            "operation_key": stage_key,
            "result_bundle_asset_id": bundle_asset_id,
            "input_snapshot_hash": request["run_snapshot_hash"],
        },
    )
    if response.get("accepted") is not True:
        raise StructureWorkerError(
            "CANDIDATE_STAGE_ERROR",
            "Core rejected Candidate staging",
        )
    rows = response.get("staged_items")
    if not isinstance(rows, list) or not rows:
        raise StructureWorkerError(
            "CANDIDATE_STAGE_ERROR",
            "Core returned no Candidate stage rows",
        )
    expected_ids = [
        _id(item.get("item_id"), f"items[{index}].item_id")
        for index, item in enumerate(items)
    ]
    actual_ids: list[str] = []
    row_fields = {
        "item_id",
        "candidate_id",
        "stage_status",
        "publication_eligibility",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != row_fields:
            raise StructureWorkerError(
                "CANDIDATE_STAGE_ERROR",
                f"staged_items[{index}] is not the exact closed row",
            )
        try:
            item_id = _id(
                row["item_id"], f"staged_items[{index}].item_id"
            )
            _id(
                row["candidate_id"],
                f"staged_items[{index}].candidate_id",
            )
        except StructureWorkerError as exc:
            raise StructureWorkerError(
                "CANDIDATE_STAGE_ERROR",
                f"staged_items[{index}] has an invalid Core identity",
            ) from exc
        if row["stage_status"] not in {"created", "existing"}:
            raise StructureWorkerError(
                "CANDIDATE_STAGE_ERROR",
                "successful stage row must be created/existing",
            )
        if row["publication_eligibility"] not in {
            "eligible",
            "review_only",
            "none",
        }:
            raise StructureWorkerError(
                "CANDIDATE_STAGE_ERROR",
                "stage row publication_eligibility is invalid",
            )
        actual_ids.append(item_id)
    if (
        len(actual_ids) != len(set(actual_ids))
        or actual_ids != expected_ids
    ):
        raise StructureWorkerError(
            "CANDIDATE_STAGE_ERROR",
            "stage rows are not ordered one-to-one with emitted Candidate items",
        )
    context.stage_response = deepcopy(dict(response))
    _record_event(context, response, "host.candidate.stage/v1")
    return stage_key, actual_ids


def _receipt(
    request: Mapping[str, Any],
    capability: str,
    package_hash: str,
    release_id: str,
    bundle: Mapping[str, Any] | None,
    staged_items: list[str],
) -> dict[str, Any]:
    if len(staged_items) != len(set(staged_items)):
        raise StructureWorkerError(
            "RESULT_CONTRACT_ERROR",
            "provenance staged_items must be unique",
        )
    receipt: dict[str, Any] = {
        "schema": "provenance-receipt/v1",
        "receipt_id": request["provenance_receipt_id"],
        "plugin_id": PLUGIN_ID,
        "release_id": release_id,
        "package_hash": package_hash,
        "capability_id": capability,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "bundle_id": bundle["bundle_id"] if bundle is not None else None,
        "bundle_hash": (
            hash_json("result-bundle/v1", bundle)
            if bundle is not None
            else None
        ),
        "parent_receipt_ids": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": list(staged_items),
        "created_at": request["created_at"],
    }
    receipt["receipt_hash"] = hash_json(
        "provenance-receipt/v1", receipt
    )
    _require_sdk()
    assert _verify_provenance_receipt_sdk is not None
    try:
        _verify_provenance_receipt_sdk(receipt)
    except Exception as exc:
        raise StructureWorkerError(
            "RESULT_CONTRACT_ERROR",
            f"provenance receipt: {exc}",
        ) from exc
    return receipt


def _complete(
    host: HostPort | None,
    request: Mapping[str, Any],
    context: _RunContext,
    outcome: str,
    result_bundle_asset_id: str | None,
    candidate_stage_operation_key: str | None,
    terminal_detail_asset_id: str | None,
    local_seq: int,
) -> dict[str, object]:
    if outcome not in {"succeeded", "partial", "failed", "cancelled"}:
        raise TerminalContractError(
            "JOB_COMPLETE_ERROR", "unsupported job completion outcome"
        )
    if local_seq <= context.last_local_seq:
        raise TerminalContractError(
            "JOB_COMPLETE_ERROR",
            "terminal local_seq is not strictly monotonic",
        )
    try:
        response = _host_call(
            host,
            "host.job.complete/v1",
            {
                "operation_key": context.request_hash,
                "worker_run_id": request["worker_run_id"],
                "outcome": outcome,
                "result_bundle_asset_id": result_bundle_asset_id,
                "candidate_stage_operation_key": candidate_stage_operation_key,
                "terminal_detail_asset_id": terminal_detail_asset_id,
                "local_seq": local_seq,
            },
        )
        if response.get("accepted") is not True:
            raise TerminalContractError(
                "JOB_COMPLETE_ERROR", "Core rejected job completion"
            )
        if (
            response.get("attempt_state") != outcome
            or response.get("step_state") != outcome
            or response.get("job_state") != outcome
        ):
            raise TerminalContractError(
                "JOB_COMPLETE_ERROR",
                "Host terminal states do not match outcome",
            )
        if (
            response.get("provenance_receipt_id")
            != request["provenance_receipt_id"]
        ):
            raise TerminalContractError(
                "JOB_COMPLETE_ERROR",
                "Host terminal receipt identity mismatch",
            )
        sequence = response.get("job_event_seq")
        high_water = response.get("core_event_high_water")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= context.last_job_event_seq
            or isinstance(high_water, bool)
            or not isinstance(high_water, int)
            or high_water < sequence
        ):
            raise TerminalContractError(
                "JOB_COMPLETE_ERROR",
                "Host terminal event high-water is invalid",
            )
        if (
            outcome == "succeeded"
            and not isinstance(result_bundle_asset_id, str)
        ):
            raise TerminalContractError(
                "JOB_COMPLETE_ERROR",
                "succeeded completion requires a result Bundle Asset",
            )
        context.last_job_event_seq = sequence
        context.last_local_seq = local_seq
        return response
    except TerminalContractError:
        raise
    except Exception as exc:
        raise TerminalContractError(
            "JOB_COMPLETE_ERROR", str(exc)
        ) from exc


def _checkpoint(
    host: HostPort | None,
    request: Mapping[str, Any],
    context: _RunContext,
    state_asset_id: str,
    state_asset_hash: str,
) -> dict[str, Any]:
    if not context.checkpoint_ids:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "Core did not issue a checkpoint identity",
        )
    checkpoint: dict[str, Any] = {
        "schema": "checkpoint/v1",
        "checkpoint_id": context.checkpoint_ids[0],
        "checkpoint_seq": 1,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "source_attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "replay_policy": "checkpoint_resume",
        "completed_units": 1,
        "total_units": request["total_units"],
        "unit_set_hash": state_asset_hash,
        "state_asset_id": state_asset_id,
        "created_at": request["created_at"],
    }
    checkpoint["checkpoint_hash"] = hash_json(
        "checkpoint/v1", checkpoint
    )
    _require_sdk()
    assert _verify_checkpoint_sdk is not None
    try:
        _verify_checkpoint_sdk(
            checkpoint,
            expected_snapshot_hash=request["run_snapshot_hash"],
            previous_seq=None,
        )
    except Exception as exc:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID", str(exc)
        ) from exc
    checkpoint_bytes = _json_bytes(checkpoint)
    checkpoint_asset_id = _upload(
        host,
        context,
        checkpoint_bytes,
        "application/json",
        "rebind-checkpoint",
    )
    response = _host_call(
        host,
        "host.checkpoint.commit/v1",
        {
            "operation_key": context.request_hash,
            "checkpoint_asset_id": checkpoint_asset_id,
        },
    )
    if (
        response.get("accepted") is not True
        or response.get("checkpoint_id") != checkpoint["checkpoint_id"]
        or response.get("completed_units") != 1
        or response.get("total_units") != request["total_units"]
    ):
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "Host checkpoint acknowledgement changed Core identity",
        )
    _record_event(context, response, "host.checkpoint.commit/v1")
    context.last_checkpoint_seq = 1
    return {
        "checkpoint": checkpoint,
        "checkpoint_asset_id": checkpoint_asset_id,
        "checkpoint_asset_hash": _hash_bytes(checkpoint_bytes),
        "state_asset_id": state_asset_id,
        "state_asset_hash": state_asset_hash,
    }


def _source_ref(
    request: Mapping[str, Any],
    revision_id: str | None = None,
    content_hash: str | None = None,
) -> dict[str, object]:
    revision = revision_id or str(
        request.get("revision_id") or request.get("source_revision_id")
    )
    content = (
        content_hash
        or request.get("canonical_text_hash")
        or request.get("source_canonical_text_hash")
    )
    return {
        "workspace_id": request["workspace_id"],
        "source_type": "canonical_revision",
        "source_id": request["document_id"],
        "revision_or_hash": revision + ":" + str(content),
    }


def _target_ref(
    request: Mapping[str, Any], revision_id: str, content_hash: str
) -> dict[str, object]:
    return {
        "workspace_id": request["workspace_id"],
        "source_type": "canonical_revision",
        "source_id": request["document_id"],
        "revision_or_hash": revision_id + ":" + content_hash,
    }


def _item_id(
    prefix: str, request_hash: str, extra: str = ""
) -> str:
    return (
        prefix
        + "-"
        + request_hash[:40]
        + (("-" + extra) if extra else "")
    )


def _safe_error_message(error: BaseException) -> str:
    message = (
        str(error).replace("\r", " ").replace("\n", " ").strip()
    )
    return message[:240] or type(error).__name__


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "INTERNAL_ERROR"


def _state(
    request: Mapping[str, Any],
    context: _RunContext,
    *,
    status: str,
    result_bundle_asset_id: str,
    result_bundle_hash: str,
    report_asset_id: str,
    report_hash: str,
) -> dict[str, Any]:
    source_binding = {
        "workspace_id": request["workspace_id"],
        "document_id": request["document_id"],
        "revision_id": request["source_revision_id"],
        "asset_id": request["source_canonical_asset_id"],
        "canonical_text_hash": request[
            "source_canonical_text_hash"
        ],
        "nodes_hash": hash_json(
            "source-structure-nodes/v1", request["source_nodes"]
        ),
    }
    target_binding = {
        "workspace_id": request["workspace_id"],
        "document_id": request["document_id"],
        "revision_id": request["target_revision_id"],
        "asset_id": request["target_canonical_asset_id"],
        "canonical_text_hash": request[
            "target_canonical_text_hash"
        ],
        "nodes_hash": hash_json(
            "source-structure-nodes/v1", request["target_nodes"]
        ),
    }
    return {
        "schema": "source-structure-rebind-state/v1",
        "capability_id": CAPABILITY_REBIND_INSPECT,
        "status": status,
        "request_binding_hash": context.binding_hash,
        "run_snapshot_hash": request["run_snapshot_hash"],
        "source_binding": source_binding,
        "target_binding": target_binding,
        "evidence_items_hash": hash_json(
            "source-evidence-items/v1", request["evidence_items"]
        ),
        "result_bundle_asset_id": result_bundle_asset_id,
        "result_bundle_hash": result_bundle_hash,
        "report_asset_id": report_asset_id,
        "report_hash": report_hash,
        "provenance_receipt_id": request["provenance_receipt_id"],
        "created_at": request["created_at"],
    }


def _validate_state(
    state: Any,
    request: Mapping[str, Any],
    context: _RunContext,
) -> dict[str, Any]:
    fields = {
        "schema",
        "capability_id",
        "status",
        "request_binding_hash",
        "run_snapshot_hash",
        "source_binding",
        "target_binding",
        "evidence_items_hash",
        "result_bundle_asset_id",
        "result_bundle_hash",
        "report_asset_id",
        "report_hash",
        "provenance_receipt_id",
        "created_at",
    }
    if not isinstance(state, Mapping) or set(state) != fields:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID", "rebind state fields are not exact"
        )
    value = dict(state)
    if (
        value["schema"] != "source-structure-rebind-state/v1"
        or value["capability_id"] != CAPABILITY_REBIND_INSPECT
        or value["status"] not in {"completed", "cancelled"}
    ):
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "rebind state identity/status is invalid",
        )
    expected = _state(
        request,
        context,
        status=value["status"],
        result_bundle_asset_id=value["result_bundle_asset_id"],
        result_bundle_hash=value["result_bundle_hash"],
        report_asset_id=value["report_asset_id"],
        report_hash=value["report_hash"],
    )
    if value != expected:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "rebind state is bound to another request/source/snapshot",
        )
    for field in ("result_bundle_asset_id", "report_asset_id"):
        _id(value[field], f"state.{field}")
    for field in (
        "result_bundle_hash",
        "report_hash",
        "request_binding_hash",
        "run_snapshot_hash",
        "evidence_items_hash",
    ):
        _hash(value[field], f"state.{field}")
    return value


def _load_resume_result(
    host: HostPort | None,
    request: Mapping[str, Any],
    context: _RunContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint_bytes = _read_asset(
        host,
        request["resume_checkpoint_asset_id"],
        request["resume_checkpoint_asset_hash"],
    )
    checkpoint = _strict_json(
        checkpoint_bytes, code="CHECKPOINT_INVALID"
    )
    _require_sdk()
    assert _verify_checkpoint_sdk is not None
    try:
        _verify_checkpoint_sdk(
            checkpoint,
            expected_snapshot_hash=request["run_snapshot_hash"],
        )
    except Exception as exc:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            f"checkpoint failed public verification: {exc}",
        ) from exc
    if (
        checkpoint.get("checkpoint_id") not in context.checkpoint_ids
        or checkpoint.get("job_id") != request["job_id"]
        or checkpoint.get("step_id") != request["step_id"]
        or checkpoint.get("source_attempt_id") != request["attempt_id"]
        or checkpoint.get("lease_epoch") != request["lease_epoch"]
        or checkpoint.get("run_snapshot_hash")
        != request["run_snapshot_hash"]
        or checkpoint.get("state_asset_id")
        != request["resume_state_asset_id"]
        or checkpoint.get("unit_set_hash")
        != request["resume_state_asset_hash"]
    ):
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "checkpoint is bound to another Core run/state",
        )
    state_bytes = _read_asset(
        host,
        request["resume_state_asset_id"],
        request["resume_state_asset_hash"],
    )
    state = _validate_state(
        _strict_json(state_bytes, code="CHECKPOINT_INVALID"),
        request,
        context,
    )
    result_bytes = _read_asset(
        host,
        state["result_bundle_asset_id"],
        state["result_bundle_hash"],
    )
    result = _strict_json(
        result_bytes, code="CHECKPOINT_INVALID"
    )
    if not isinstance(result, dict):
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "resumed result Bundle is not an object",
        )
    assert _verify_result_bundle_sdk is not None
    try:
        _verify_result_bundle_sdk(
            result,
            snapshot_hash_value=request["run_snapshot_hash"],
        )
    except Exception as exc:
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            f"resumed result Bundle failed public verification: {exc}",
        ) from exc
    producer = result.get("producer")
    if (
        result.get("contract_id") != "diagnostic-bundle/v1"
        or not isinstance(producer, Mapping)
        or producer.get("plugin_id") != PLUGIN_ID
        or producer.get("release_id") != RELEASE_ID
        or producer.get("capability_id")
        != CAPABILITY_REBIND_INSPECT
        or result.get("provenance_receipt_id")
        != request["provenance_receipt_id"]
        or _hash_bytes(result_bytes) != state["result_bundle_hash"]
    ):
        raise StructureWorkerError(
            "CHECKPOINT_INVALID",
            "resumed result Bundle identity/hash is invalid",
        )
    return result, state


class StructurePlugin:
    """Deterministic worker with only injected Core HostPort access."""

    plugin_id = PLUGIN_ID
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self.package_hash, self.release_id = _identity()
        self.last_stage_response: dict[str, object] | None = None
        self.last_receipt: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None

    def _failure_bundle(
        self,
        request: Mapping[str, Any],
        host: HostPort | None,
        capability: str,
        error: BaseException,
        context: _RunContext | None,
    ) -> dict[str, Any] | None:
        if isinstance(error, TerminalContractError):
            raise error
        try:
            for field in (
                "job_id",
                "step_id",
                "attempt_id",
                "worker_run_id",
                "provenance_receipt_id",
                "workspace_id",
            ):
                _id(request.get(field), field)
            _integer(request.get("lease_epoch"), "lease_epoch", 1)
            _hash(
                request.get("run_snapshot_hash"),
                "run_snapshot_hash",
            )
            if (
                not isinstance(request.get("created_at"), str)
                or _TIME.fullmatch(str(request["created_at"])) is None
            ):
                raise StructureWorkerError(
                    "INPUT_INVALID",
                    "created_at is unavailable for terminal failure",
                )
            if context is None:
                checkpoints = request.get("checkpoint_ids")
                checkpoint_ids = (
                    tuple(checkpoints)
                    if isinstance(checkpoints, list)
                    else ()
                )
                context = _RunContext(
                    request_hash=hash_json(
                        capability + "-failure-request/v1",
                        dict(request),
                    ),
                    binding_hash=_ZERO_HASH,
                    checkpoint_ids=checkpoint_ids,
                )
            code = _error_code(error)
            message = _safe_error_message(error)
            detail = {
                "schema": "source-structure-failure/v1",
                "code": code,
                "message": message,
                "retryable": bool(
                    getattr(error, "retryable", False)
                ),
            }
            detail_bytes = _json_bytes(detail)
            detail_asset_id = _upload(
                host,
                context,
                detail_bytes,
                "application/json",
                "failure-detail",
            )
            event_local = context.last_local_seq + 1
            _event(
                host,
                context,
                "source-structure.failed",
                detail_asset_id,
                event_local,
            )
            item = {
                "schema": "diagnostic-item/v1",
                "item_id": _item_id(
                    "diagnostic-structure", context.request_hash
                ),
                "severity": "error",
                "code": code,
                "message": message,
                "details_asset_id": detail_asset_id,
                "details_hash": _hash_bytes(detail_bytes),
                "source_refs": [],
                "status": "failed",
            }
            bundle = _bundle(
                request,
                capability,
                "diagnostic-bundle/v1",
                "diagnostic",
                _item_id(
                    "bundle-structure-failure",
                    context.request_hash,
                ),
                [item],
                self.release_id,
                partial=True,
            )
            bundle_bytes = _json_bytes(bundle)
            bundle_asset_id = _upload(
                host,
                context,
                bundle_bytes,
                "application/json",
                "failure-bundle",
            )
            self.last_receipt = _receipt(
                request,
                capability,
                self.package_hash,
                self.release_id,
                bundle,
                [],
            )
            self.last_stage_response = None
            _complete(
                host,
                request,
                context,
                "failed",
                bundle_asset_id,
                None,
                detail_asset_id,
                context.last_local_seq + 1,
            )
            return bundle
        except TerminalContractError:
            raise
        except Exception as terminal_error:
            raise StructureWorkerError(
                "FAILURE_TERMINAL_ERROR",
                _safe_error_message(terminal_error),
            ) from terminal_error

    def cancel(
        self, request: Mapping[str, Any]
    ) -> dict[str, object]:
        value, context = _context(
            request, CAPABILITY_REBIND_INSPECT
        )
        accepted = _DISPATCHER.cancel(
            value["worker_run_id"], context.binding_hash
        )
        return {
            "accepted": accepted,
            "worker_run_id": value["worker_run_id"],
        }

    def revise(
        self,
        request: Mapping[str, Any],
        host: HostPort | None = None,
    ) -> dict[str, Any]:
        value = _validate_context(request, CAPABILITY_REVISE)
        text = _load_text(value, host)
        text_hash = sha256_text(text)
        nodes = _nodes(value, text, "nodes")
        try:
            revised_nodes = apply_structure_operations(
                nodes,
                value["operations"],
                text_length=len(text),
            )
        except Exception as exc:
            raise StructureWorkerError(
                "STRUCTURE_INVALID", str(exc)
            ) from exc
        request_hash = _request_hash(value, CAPABILITY_REVISE)
        structure_hash = hash_json(
            "source-structure/v1",
            {
                "nodes": revised_nodes,
                "operations": value["operations"],
                "base_revision_id": value["revision_id"],
                "base_canonical_text_hash": text_hash,
            },
        )
        payload = {
            "schema": "source-structure-patch/v1",
            "workspace_id": value["workspace_id"],
            "document_id": value["document_id"],
            "base_revision_id": value["revision_id"],
            "base_canonical_text_hash": text_hash,
            "operations": value["operations"],
            "nodes": revised_nodes,
            "structure_hash": structure_hash,
        }
        payload_bytes = _json_bytes(payload)
        payload_hash = _hash_bytes(payload_bytes)
        upload_context = _RunContext(
            request_hash,
            _binding_hash(value, CAPABILITY_REVISE),
            tuple(value["checkpoint_ids"]),
        )
        asset_id = _upload(
            host,
            upload_context,
            payload_bytes,
            "application/json",
            "structure-patch",
        )
        candidate = {
            "schema": "candidate-item/v1",
            "item_id": _item_id(
                "candidate-structure", request_hash
            ),
            "item_kind": "node_structure",
            "target": {
                "workspace_id": value["workspace_id"],
                "entity_kind": "node_structure",
                "entity_id": value["document_id"],
            },
            "mutation": {
                "mode": "structure_patch",
                "payload_schema": "source-structure-patch/v1",
                "payload_hash": payload_hash,
            },
            "payload_asset_id": asset_id,
            "base": {
                "revision_id": value["revision_id"],
                "content_hash": text_hash,
            },
            "write_set": [
                {
                    "workspace_id": value["workspace_id"],
                    "entity_kind": "node_structure",
                    "entity_id": value["document_id"],
                    "revision_id": value["revision_id"],
                    "content_hash": text_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [
                _source_ref(value, content_hash=text_hash)
            ],
            "status": "complete",
        }
        return _bundle(
            value,
            CAPABILITY_REVISE,
            "candidate-batch/v1",
            "candidate_batch",
            _item_id("bundle-structure", request_hash),
            [candidate],
            self.release_id,
        )

    def search(
        self,
        request: Mapping[str, Any],
        host: HostPort | None = None,
    ) -> dict[str, Any]:
        value = _validate_context(request, CAPABILITY_SEARCH)
        text = _load_text(value, host)
        text_hash = sha256_text(text)
        nodes = _nodes(value, text, "nodes")
        try:
            matches = find_text_matches(
                canonical_text=text,
                query=value["query"],
                workspace_id=value["workspace_id"],
                document_id=value["document_id"],
                revision_id=value["revision_id"],
                canonical_text_hash=text_hash,
                nodes=nodes,
                case_sensitive=value["case_sensitive"],
                max_matches=value["max_matches"],
            )
        except Exception as exc:
            raise StructureWorkerError(
                "SEARCH_INVALID", str(exc)
            ) from exc
        detail = {
            "schema": "source-evidence-search/v1",
            "workspace_id": value["workspace_id"],
            "document_id": value["document_id"],
            "revision_id": value["revision_id"],
            "canonical_text_hash": text_hash,
            "query": value["query"],
            "case_sensitive": value["case_sensitive"],
            "matches": matches,
        }
        request_hash = _request_hash(value, CAPABILITY_SEARCH)
        upload_context = _RunContext(
            request_hash,
            _binding_hash(value, CAPABILITY_SEARCH),
            tuple(value["checkpoint_ids"]),
        )
        data = _json_bytes(detail)
        asset_id = _upload(
            host,
            upload_context,
            data,
            "application/json",
            "evidence-search",
        )
        artifact = {
            "schema": "artifact-item/v1",
            "item_id": _item_id(
                "artifact-search", request_hash
            ),
            "artifact_kind": "source-evidence-search/v1",
            "payload_asset_id": asset_id,
            "payload_hash": _hash_bytes(data),
            "mime": "application/json",
            "source_refs": [
                _source_ref(value, content_hash=text_hash)
            ],
            "status": "complete",
        }
        return _bundle(
            value,
            CAPABILITY_SEARCH,
            "artifact-bundle/v1",
            "artifact",
            _item_id("bundle-search", request_hash),
            [artifact],
            self.release_id,
        )

    def _rebind_context(
        self,
        request: Mapping[str, Any],
        host: HostPort | None,
        capability: str,
    ) -> tuple[
        dict[str, Any],
        list[Mapping[str, Any]],
        dict[str, Any],
    ]:
        value = _validate_context(request, capability)
        source_text = _load_text(value, host, "source")
        target_text = _load_text(value, host, "target")
        source_nodes = _nodes(
            value, source_text, "source_nodes"
        )
        target_nodes = _nodes(
            value, target_text, "target_nodes"
        )
        raw_items = value["evidence_items"]
        try:
            items = [
                rebind_evidence(
                    item,
                    source_text,
                    target_text,
                    source_nodes,
                    target_nodes,
                    source_canonical_text_hash=value[
                        "source_canonical_text_hash"
                    ],
                    target_canonical_text_hash=value[
                        "target_canonical_text_hash"
                    ],
                    target_revision_id=value[
                        "target_revision_id"
                    ],
                    expected_workspace_id=value["workspace_id"],
                    expected_document_id=value["document_id"],
                    expected_source_revision_id=value[
                        "source_revision_id"
                    ],
                )
                for item in raw_items
            ]
            report = build_rebind_report(
                workspace_id=value["workspace_id"],
                document_id=value["document_id"],
                source_revision_id=value["source_revision_id"],
                source_canonical_text_hash=value[
                    "source_canonical_text_hash"
                ],
                target_revision_id=value["target_revision_id"],
                target_canonical_text_hash=value[
                    "target_canonical_text_hash"
                ],
                items=items,
            )
            validate_rebind_report(report)
        except Exception as exc:
            raise StructureWorkerError(
                "EVIDENCE_LINEAGE_INVALID", str(exc)
            ) from exc
        return value, raw_items, report

    def _inspect_bundle(
        self,
        request: Mapping[str, Any],
        host: HostPort | None,
    ) -> tuple[dict[str, Any], str, str]:
        value, _raw_items, report = self._rebind_context(
            request, host, CAPABILITY_REBIND_INSPECT
        )
        request_hash = _request_hash(
            value, CAPABILITY_REBIND_INSPECT
        )
        upload_context = _RunContext(
            request_hash,
            _binding_hash(
                value, CAPABILITY_REBIND_INSPECT
            ),
            tuple(value["checkpoint_ids"]),
        )
        data = _json_bytes(report)
        detail_id = _upload(
            host,
            upload_context,
            data,
            "application/json",
            "rebind-report",
        )
        classes = {
            item["classification"] for item in report["items"]
        }
        severity = (
            "info"
            if classes.issubset({"unchanged"})
            else "warning"
        )
        diagnostic = {
            "schema": "diagnostic-item/v1",
            "item_id": _item_id(
                "diagnostic-rebind", request_hash
            ),
            "severity": severity,
            "code": "SOURCE_EVIDENCE_REBIND",
            "message": (
                "Evidence rebind inspection completed; "
                "no Candidate was staged"
            ),
            "details_asset_id": detail_id,
            "details_hash": _hash_bytes(data),
            "source_refs": [
                _source_ref(
                    value,
                    value["source_revision_id"],
                    report["source_canonical_text_hash"],
                ),
                _target_ref(
                    value,
                    value["target_revision_id"],
                    report["target_canonical_text_hash"],
                ),
            ],
            "status": "complete",
        }
        bundle = _bundle(
            value,
            CAPABILITY_REBIND_INSPECT,
            "diagnostic-bundle/v1",
            "diagnostic",
            _item_id(
                "bundle-rebind-inspect", request_hash
            ),
            [diagnostic],
            self.release_id,
        )
        return bundle, detail_id, _hash_bytes(data)

    def rebind_inspect(
        self,
        request: Mapping[str, Any],
        host: HostPort | None = None,
    ) -> dict[str, Any]:
        bundle, _report_asset_id, _report_hash = (
            self._inspect_bundle(request, host)
        )
        return bundle

    def rebind_propose(
        self,
        request: Mapping[str, Any],
        host: HostPort | None = None,
    ) -> dict[str, Any]:
        value, raw_items, report = self._rebind_context(
            request, host, CAPABILITY_REBIND_PROPOSE
        )
        selected = value["selected_evidence_ids"] or [
            item["evidence_id"]
            for item in report["items"]
            if item["classification"] == "rebound"
        ]
        by_id = {
            item["evidence_id"]: item for item in report["items"]
        }
        raw_by_id = {
            str(item.get("evidence_id")): item
            for item in raw_items
            if isinstance(item, Mapping)
            and item.get("evidence_id") is not None
        }
        for evidence_id in selected:
            if (
                evidence_id not in by_id
                or by_id[evidence_id]["classification"]
                != "rebound"
            ):
                raise StructureWorkerError(
                    "INPUT_INVALID",
                    "only deterministic rebound items may become Candidates",
                )
        request_hash = _request_hash(
            value, CAPABILITY_REBIND_PROPOSE
        )
        upload_context = _RunContext(
            request_hash,
            _binding_hash(
                value, CAPABILITY_REBIND_PROPOSE
            ),
            tuple(value["checkpoint_ids"]),
        )
        candidates: list[dict[str, Any]] = []
        known_parents = set(
            value["known_parent_candidate_ids"]
        )
        for evidence_id in selected:
            report_item = by_id[evidence_id]
            raw_item = raw_by_id.get(evidence_id, {})
            predecessor = raw_item.get("parent_candidate_id")
            parents = (
                [] if predecessor is None else [predecessor]
            )
            if (
                predecessor is not None
                and predecessor not in known_parents
            ):
                raise StructureWorkerError(
                    "INPUT_INVALID",
                    "parent Candidate is not declared as Core-known",
                )
            payload = {
                "schema": "source-evidence-successor/v1",
                "evidence_id": evidence_id,
                "predecessor_span": report_item[
                    "source_span"
                ],
                "successor_span": report_item["target_span"],
                "source_revision_id": value[
                    "source_revision_id"
                ],
                "target_revision_id": value[
                    "target_revision_id"
                ],
                "source_canonical_text_hash": report[
                    "source_canonical_text_hash"
                ],
                "target_canonical_text_hash": report[
                    "target_canonical_text_hash"
                ],
            }
            payload_bytes = _json_bytes(payload)
            payload_hash = _hash_bytes(payload_bytes)
            asset_id = _upload(
                host,
                upload_context,
                payload_bytes,
                "application/json",
                "evidence-successor-" + evidence_id,
            )
            relation_id = "evidence:" + evidence_id
            candidates.append(
                {
                    "schema": "candidate-item/v1",
                    "item_id": _item_id(
                        "candidate-evidence",
                        request_hash,
                        evidence_id,
                    ),
                    "item_kind": "relation_set",
                    "target": {
                        "workspace_id": value["workspace_id"],
                        "entity_kind": "relation_set",
                        "entity_id": relation_id,
                    },
                    "mutation": {
                        "mode": "relation_patch",
                        "payload_schema": (
                            "source-evidence-successor/v1"
                        ),
                        "payload_hash": payload_hash,
                    },
                    "payload_asset_id": asset_id,
                    "base": {
                        "revision_id": value[
                            "target_revision_id"
                        ],
                        "content_hash": report[
                            "target_canonical_text_hash"
                        ],
                    },
                    "write_set": [
                        {
                            "workspace_id": value[
                                "workspace_id"
                            ],
                            "entity_kind": "relation_set",
                            "entity_id": relation_id,
                            "revision_id": value[
                                "target_revision_id"
                            ],
                            "content_hash": report[
                                "target_canonical_text_hash"
                            ],
                        }
                    ],
                    "parent_candidate_ids": parents,
                    "source_refs": [
                        _source_ref(
                            value,
                            value["source_revision_id"],
                            report[
                                "source_canonical_text_hash"
                            ],
                        ),
                        _target_ref(
                            value,
                            value["target_revision_id"],
                            report[
                                "target_canonical_text_hash"
                            ],
                        ),
                    ],
                    "status": "complete",
                }
            )
        return _bundle(
            value,
            CAPABILITY_REBIND_PROPOSE,
            "candidate-batch/v1",
            "candidate_batch",
            _item_id(
                "bundle-rebind-propose", request_hash
            ),
            candidates,
            self.release_id,
        )

    def _finalize(
        self,
        request: Mapping[str, Any],
        capability: str,
        context: _RunContext,
        bundle: dict[str, Any],
        host: HostPort | None,
        *,
        stage_candidate: bool,
    ) -> dict[str, Any]:
        bundle_bytes = _json_bytes(bundle)
        bundle_asset_id = _upload(
            host,
            context,
            bundle_bytes,
            "application/json",
            "result-bundle",
        )
        _event(
            host,
            context,
            "source-structure.result",
            bundle_asset_id,
            2,
        )
        stage_key: str | None = None
        staged_ids: list[str] = []
        if stage_candidate and bundle["items"]:
            stage_key, staged_ids = _stage(
                host,
                request,
                context,
                bundle_asset_id,
                bundle["items"],
            )
            self.last_stage_response = deepcopy(
                context.stage_response
            )
        else:
            self.last_stage_response = None
        self.last_receipt = _receipt(
            request,
            capability,
            self.package_hash,
            self.release_id,
            bundle,
            staged_ids,
        )
        _complete(
            host,
            request,
            context,
            "succeeded",
            bundle_asset_id,
            stage_key,
            None,
            3,
        )
        return bundle

    def _run_inspect(
        self,
        request: Mapping[str, Any],
        host: HostPort | None,
        context: _RunContext,
        active: _ActiveRun,
    ) -> dict[str, Any] | None:
        _event(
            host,
            context,
            "source-structure.rebind.inspect.started",
            None,
            1,
        )
        (
            bundle,
            report_asset_id,
            report_hash,
        ) = self._inspect_bundle(request, host)
        bundle_bytes = _json_bytes(bundle)
        bundle_asset_id = _upload(
            host,
            context,
            bundle_bytes,
            "application/json",
            "result-bundle",
        )
        bundle_hash = _hash_bytes(bundle_bytes)
        _event(
            host,
            context,
            "source-structure.rebind.inspect.result",
            bundle_asset_id,
            2,
        )
        status = (
            "cancelled"
            if active.cancelled.is_set()
            else "completed"
        )
        state = _state(
            request,
            context,
            status=status,
            result_bundle_asset_id=bundle_asset_id,
            result_bundle_hash=bundle_hash,
            report_asset_id=report_asset_id,
            report_hash=report_hash,
        )
        state_bytes = _json_bytes(state)
        state_asset_id = _upload(
            host,
            context,
            state_bytes,
            "application/json",
            "rebind-state",
        )
        self.last_checkpoint = _checkpoint(
            host,
            request,
            context,
            state_asset_id,
            _hash_bytes(state_bytes),
        )
        if status == "cancelled":
            self.last_receipt = _receipt(
                request,
                CAPABILITY_REBIND_INSPECT,
                self.package_hash,
                self.release_id,
                None,
                [],
            )
            self.last_stage_response = None
            _complete(
                host,
                request,
                context,
                "cancelled",
                None,
                None,
                None,
                3,
            )
            return None
        self.last_receipt = _receipt(
            request,
            CAPABILITY_REBIND_INSPECT,
            self.package_hash,
            self.release_id,
            bundle,
            [],
        )
        self.last_stage_response = None
        _complete(
            host,
            request,
            context,
            "succeeded",
            bundle_asset_id,
            None,
            None,
            3,
        )
        return bundle

    def _resume_inspect(
        self,
        request: Mapping[str, Any],
        host: HostPort | None,
        context: _RunContext,
        active: _ActiveRun,
    ) -> dict[str, Any] | None:
        _event(
            host,
            context,
            "source-structure.rebind.inspect.resumed",
            request["resume_state_asset_id"],
            1,
        )
        bundle, state = _load_resume_result(
            host, request, context
        )
        if active.cancelled.is_set():
            self.last_receipt = _receipt(
                request,
                CAPABILITY_REBIND_INSPECT,
                self.package_hash,
                self.release_id,
                None,
                [],
            )
            _complete(
                host,
                request,
                context,
                "cancelled",
                None,
                None,
                None,
                2,
            )
            return None
        _event(
            host,
            context,
            "source-structure.rebind.inspect.result",
            state["result_bundle_asset_id"],
            2,
        )
        self.last_receipt = _receipt(
            request,
            CAPABILITY_REBIND_INSPECT,
            self.package_hash,
            self.release_id,
            bundle,
            [],
        )
        self.last_stage_response = None
        _complete(
            host,
            request,
            context,
            "succeeded",
            state["result_bundle_asset_id"],
            None,
            None,
            3,
        )
        return bundle

    def run(
        self,
        request: Mapping[str, Any],
        host: HostPort | None = None,
    ) -> dict[str, Any] | None:
        value: Mapping[str, Any] = (
            request if isinstance(request, Mapping) else {}
        )
        capability: str | None = None
        normalized: dict[str, Any] | None = None
        context: _RunContext | None = None
        active: _ActiveRun | None = None
        try:
            if not isinstance(request, Mapping):
                raise StructureWorkerError(
                    "INPUT_INVALID",
                    "request must be an object",
                )
            value = dict(request)
            if "request_asset_id" in value:
                if set(value) != {
                    "request_asset_id",
                    "request_asset_hash",
                }:
                    raise StructureWorkerError(
                        "INPUT_INVALID",
                        "request_asset envelope is closed",
                    )
                raw = _read_asset(
                    host,
                    _id(
                        value["request_asset_id"],
                        "request_asset_id",
                    ),
                    _hash(
                        value["request_asset_hash"],
                        "request_asset_hash",
                    ),
                )
                loaded = _strict_json(raw)
                if not isinstance(loaded, dict):
                    raise StructureWorkerError(
                        "INPUT_INVALID",
                        "request Asset must contain an object",
                    )
                value = loaded
            capability = value.get("capability_id")
            if capability in (None, ""):
                capability = {
                    schema: child
                    for child, schema in _SCHEMAS.items()
                }.get(value.get("schema"))
            if capability not in CAPABILITIES:
                raise StructureWorkerError(
                    "CAPABILITY_UNKNOWN",
                    "unknown source-structure capability",
                )
            normalized, context = _context(value, capability)
            operation = normalized["operation"]
            if operation == "cancel":
                return self.cancel(normalized)
            if operation in {"run", "resume"}:
                active = _DISPATCHER.begin(
                    normalized["worker_run_id"],
                    context.binding_hash,
                )
            if operation == "resume":
                assert active is not None
                return self._resume_inspect(
                    normalized, host, context, active
                )
            if (
                operation == "run"
                and capability != CAPABILITY_REBIND_INSPECT
            ):
                _event(
                    host,
                    context,
                    "source-structure.started",
                    None,
                    1,
                )
            if capability == CAPABILITY_REVISE:
                result = self.revise(normalized, host)
            elif capability == CAPABILITY_SEARCH:
                result = self.search(normalized, host)
            elif capability == CAPABILITY_REBIND_INSPECT:
                assert active is not None
                return self._run_inspect(
                    normalized, host, context, active
                )
            else:
                result = self.rebind_propose(
                    normalized, host
                )
            if operation == "validate":
                self.last_stage_response = None
                self.last_receipt = None
                return result
            return self._finalize(
                normalized,
                capability,
                context,
                result,
                host,
                stage_candidate=capability
                in {
                    CAPABILITY_REVISE,
                    CAPABILITY_REBIND_PROPOSE,
                },
            )
        except TerminalContractError:
            raise
        except StructureWorkerError as exc:
            if capability in CAPABILITIES:
                return self._failure_bundle(
                    normalized or value,
                    host,
                    capability,
                    exc,
                    context,
                )
            return None
        except Exception as exc:
            if capability in CAPABILITIES:
                return self._failure_bundle(
                    normalized or value,
                    host,
                    capability,
                    StructureWorkerError(
                        "INTERNAL_ERROR",
                        _safe_error_message(exc),
                    ),
                    context,
                )
            return None
        finally:
            if active is not None:
                _DISPATCHER.finish(active)


_DESCRIPTOR_DEFINITIONS = {
    CAPABILITY_REVISE: {
        "input_schema": "source.structure.revise-request/v1",
        "output_schema": "source.structure.revise-result/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "validate"],
        "accepted_data_formats": [],
    },
    CAPABILITY_SEARCH: {
        "input_schema": "source.evidence.search-request/v1",
        "output_schema": "source.evidence.search-result/v1",
        "result_contract": "artifact-bundle/v1",
        "supports": ["run"],
        "accepted_data_formats": [],
    },
    CAPABILITY_REBIND_INSPECT: {
        "input_schema": (
            "source.evidence.rebind.inspect-request/v1"
        ),
        "output_schema": (
            "source.evidence.rebind.inspect-result/v1"
        ),
        "result_contract": "diagnostic-bundle/v1",
        "supports": ["run", "resume", "cancel"],
        "accepted_data_formats": [],
    },
    CAPABILITY_REBIND_PROPOSE: {
        "input_schema": (
            "source.evidence.rebind.propose-request/v1"
        ),
        "output_schema": (
            "source.evidence.rebind.propose-result/v1"
        ),
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "validate"],
        "accepted_data_formats": [],
    },
}


def _descriptor(
    capability_id: str, release_id: str
) -> dict[str, Any]:
    if capability_id not in _DESCRIPTOR_DEFINITIONS:
        raise StructureWorkerError(
            "CAPABILITY_UNKNOWN",
            "unknown source-structure capability",
        )
    definition = _DESCRIPTOR_DEFINITIONS[capability_id]
    return {
        "schema": "capability-provider/v1",
        "capability_id": capability_id,
        "provider": {
            "plugin_id": PLUGIN_ID,
            "release_id": release_id,
        },
        "input_schema": definition["input_schema"],
        "output_schema": definition["output_schema"],
        "result_contract": definition["result_contract"],
        "supports": list(definition["supports"]),
        "deterministic": True,
        "accepted_data_formats": list(
            definition["accepted_data_formats"]
        ),
    }


def capability_descriptor(
    capability_id: str | None = None,
) -> dict[str, Any]:
    _, release_id = _identity()
    return _descriptor(
        capability_id or CAPABILITY_REVISE, release_id
    )


DESCRIPTORS = {
    capability: _descriptor(capability, RELEASE_ID)
    for capability in CAPABILITIES
}
_RUNTIME = StructurePlugin()


def main(
    request: Mapping[str, Any] | None = None,
    host: HostPort | None = None,
) -> dict[str, Any] | None:
    if request is None:
        return capability_descriptor()
    return _RUNTIME.run(request, host)


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_REBIND_INSPECT",
    "CAPABILITY_REBIND_PROPOSE",
    "CAPABILITY_REVISE",
    "CAPABILITY_SEARCH",
    "DESCRIPTORS",
    "NEEDS",
    "PACKAGE_HASH",
    "PLUGIN_ID",
    "RELEASE_ID",
    "StructurePlugin",
    "StructureWorkerError",
    "TerminalContractError",
    "capability_descriptor",
    "main",
]
