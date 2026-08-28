"""Host-bound source.import.inspect/v1 and source.import.parse/v1 worker."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import re
import threading
from typing import Any, Callable, Mapping, Protocol

# Contract operations are fail-closed on the public B0 SDK.
try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _sdk_canonical_bytes
    from plotpilot_plugin_sdk.canonical import hash_jcs as _sdk_hash_jcs
    from plotpilot_plugin_sdk.verifier import validate_rpc_result as _sdk_validate_rpc_result
except ImportError as exc:  # pragma: no cover - exercised by SDK-absent install test
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _sdk_canonical_bytes = None
    _sdk_hash_jcs = None
    _sdk_validate_rpc_result = None
else:
    _SDK_IMPORT_ERROR = None


from .contract import (
    ContractError,
    INSPECT_CAPABILITY,
    PARSE_CAPABILITY,
    build_checkpoint,
    build_provenance_receipt,
    canonical_bytes,
    hash_jcs,
    sha256_hex,
    validate_request_common,
)
from .limits import DEFAULT_LIMITS, ImportLimits, ensure_size
from .pipeline import ParsedSource, SourceImportError, parse_source


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        ...


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class ImportCancelled(WorkerError):
    def __init__(
        self,
        offset: int,
        page_index: int,
        prefix: bytes,
        *,
        phase: str = "asset_read",
        asset_eof: bool = False,
    ) -> None:
        self.offset = offset
        self.page_index = page_index
        self.prefix = prefix
        self.phase = phase
        self.asset_eof = asset_eof
        super().__init__("CANCELLED", "source import cancelled")


class _OperationDispatcher:
    """Same-runtime operation boundary for the manifest's max_concurrency=1."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, threading.Event] = {}

    def begin(self, worker_run_id: str) -> threading.Event:
        with self._lock:
            if self._active:
                raise WorkerError("WORKER_BUSY", "source import already has an active operation", retryable=True)
            event = threading.Event()
            self._active[worker_run_id] = event
            return event

    def cancel(self, worker_run_id: str) -> bool:
        with self._lock:
            event = self._active.get(worker_run_id)
            if event is None:
                return False
            event.set()
            return True

    def is_cancelled(self, worker_run_id: str, event: threading.Event) -> bool:
        with self._lock:
            return self._active.get(worker_run_id) is event and event.is_set()

    def finish(self, worker_run_id: str, event: threading.Event) -> None:
        with self._lock:
            if self._active.get(worker_run_id) is event:
                del self._active[worker_run_id]


_OPERATIONS = _OperationDispatcher()


@dataclass(frozen=True)
class AssetRead:
    data: bytes
    next_offset: int | None
    page_index: int
    page_hashes: tuple[str, ...]
    total_size: int | None


def _require_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or _sdk_canonical_bytes is None or _sdk_hash_jcs is None or _sdk_validate_rpc_result is None:
        raise WorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; source import is fail-closed") from _SDK_IMPORT_ERROR

def _id(prefix: str, request_hash: str, suffix: str = "") -> str:
    value = f"{prefix}-{request_hash[:20]}"
    return value if not suffix else f"{value}-{suffix}"


def _strict_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        raise WorkerError("HOST_CONTRACT_ERROR", f"{label} is not a Core identity")
    return value


def _strict_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WorkerError("HOST_CONTRACT_ERROR", f"{label} is not a lowercase SHA-256")
    return value


def _host_call(host: HostPort, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    if host is None or not hasattr(host, "call"):
        raise WorkerError("HOST_REQUIRED", "source import accepts only Core HostPort Asset access")
    try:
        result = host.call(method, dict(params))
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}", retryable=True) from exc
    if not isinstance(result, Mapping):
        raise WorkerError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    try:
        assert _sdk_validate_rpc_result is not None
        _sdk_validate_rpc_result(method, result)
    except Exception as exc:
        raise WorkerError("HOST_CONTRACT_ERROR", f"{method} result failed the public RPC schema: {exc}") from exc
    return dict(result)


def _request_hash(request: Mapping[str, Any]) -> str:
    return hash_jcs("source-import-request/v1", dict(request))


def _base_request(request: Mapping[str, Any]) -> dict[str, Any]:
    return {key: request[key] for key in request if not key.startswith("resume_")}


def read_core_asset(
    host: HostPort,
    asset_id: str,
    expected_sha256: str,
    *,
    page_size: int = DEFAULT_LIMITS.max_page_bytes,
    limits: ImportLimits = DEFAULT_LIMITS,
    start_offset: int = 0,
    prefix: bytes = b"",
    cancel_check: Callable[[], bool] | None = None,
) -> AssetRead:
    """Read contiguous Core Asset pages and authenticate every page."""
    _strict_id(asset_id, "asset_id")
    _strict_hash(expected_sha256, "expected_sha256")
    if isinstance(start_offset, bool) or not isinstance(start_offset, int) or start_offset < 0:
        raise WorkerError("ASSET_READ_ERROR", "start_offset is invalid")
    if start_offset != len(prefix):
        raise WorkerError("ASSET_READ_ERROR", "resume prefix length does not equal start_offset")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0 or page_size > limits.max_page_bytes:
        raise WorkerError("ASSET_READ_ERROR", "page_size is outside the bounded page profile")
    ensure_size("resume prefix", len(prefix), limits.max_original_bytes)
    offset = start_offset
    page_index = 0
    chunks: list[bytes] = []
    page_hashes: list[str] = []
    total_size: int | None = None
    while page_index < limits.max_pages:
        if cancel_check is not None and cancel_check():
            raise ImportCancelled(offset, page_index, prefix + b"".join(chunks))
        response = _host_call(host, "host.asset.read/v1", {
            "asset_id": asset_id, "offset": offset, "length": page_size,
        })
        returned_offset = response.get("offset")
        if returned_offset is not None and (
            isinstance(returned_offset, bool) or not isinstance(returned_offset, int) or returned_offset != offset
        ):
            raise WorkerError("ASSET_READ_ERROR", "Asset page returned an unexpected offset")
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise WorkerError("ASSET_READ_ERROR", "Asset page is missing base64_chunk")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise WorkerError("ASSET_READ_ERROR", "Asset page base64 is invalid") from exc
        if len(chunk) > page_size:
            raise WorkerError("ASSET_READ_ERROR", "Asset page exceeds requested length")
        returned_length = response.get("length")
        if returned_length is not None and (
            isinstance(returned_length, bool) or not isinstance(returned_length, int) or returned_length != len(chunk)
        ):
            raise WorkerError("ASSET_READ_ERROR", "Asset page length does not match bytes")
        page_total = response.get("total_size")
        if page_total is not None:
            if isinstance(page_total, bool) or not isinstance(page_total, int) or page_total < 0:
                raise WorkerError("ASSET_READ_ERROR", "Asset total_size is invalid")
            if total_size is None:
                total_size = page_total
            elif total_size != page_total:
                raise WorkerError("ASSET_READ_ERROR", "Asset total_size changed between pages")
            if offset + len(chunk) > total_size:
                raise WorkerError("ASSET_READ_ERROR", "Asset page exceeds total_size")
        page_hash = response.get("content_hash")
        if page_hash != sha256_hex(chunk):
            raise WorkerError("ASSET_READ_ERROR", "Asset page content_hash mismatch")
        expected_next = offset + len(chunk)
        next_offset = response.get("next_offset")
        if next_offset is None:
            if chunk and total_size is not None and expected_next != total_size:
                raise WorkerError("ASSET_READ_ERROR", "EOF arrived before total_size")
            if not chunk and offset != 0 and total_size not in {offset, None}:
                raise WorkerError("ASSET_READ_ERROR", "empty noninitial page is invalid")
            chunks.append(chunk)
            page_hashes.append(str(page_hash))
            offset = expected_next
            break
        if (
            isinstance(next_offset, bool) or not isinstance(next_offset, int)
            or next_offset != expected_next or next_offset <= offset
        ):
            raise WorkerError("ASSET_READ_ERROR", "Asset pages are not contiguous")
        if total_size is not None and next_offset >= total_size:
            raise WorkerError("ASSET_READ_ERROR", "EOF must use next_offset=null")
        chunks.append(chunk)
        page_hashes.append(str(page_hash))
        offset = next_offset
        if offset > limits.max_original_bytes:
            raise WorkerError("ASSET_READ_ERROR", "Asset exceeds original byte limit")
        page_index += 1
    else:
        raise WorkerError("ASSET_READ_ERROR", "Asset did not terminate within max_pages")
    data = prefix + b"".join(chunks)
    ensure_size("original Asset", len(data), limits.max_original_bytes)
    if total_size is not None and len(data) != total_size:
        raise WorkerError("ASSET_READ_ERROR", "Asset total_size does not match assembled bytes")
    if sha256_hex(data) != expected_sha256:
        raise WorkerError("ASSET_READ_ERROR", "assembled Asset hash does not match Core identity")
    return AssetRead(data, None, page_index + 1, tuple(page_hashes), total_size)


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _load_json_asset(host: HostPort, asset_id: str, expected_hash: str, *, limits: ImportLimits) -> dict[str, Any]:
    raw = read_core_asset(host, asset_id, expected_hash, page_size=limits.max_page_bytes, limits=limits).data
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint state Asset is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint state Asset must be an object")
    return value


def _resume_prefix(request: Mapping[str, Any], host: HostPort, *, limits: ImportLimits) -> tuple[int, bytes, int, tuple[str, ...], bool]:
    state_id = request.get("resume_state_asset_id")
    state_hash = request.get("resume_state_asset_hash")
    checkpoint_id = request.get("resume_checkpoint_asset_id")
    if checkpoint_id is not None:
        try:
            checkpoint_id = _strict_id(checkpoint_id, "resume_checkpoint_asset_id")
            checkpoint_hash = _strict_hash(request.get("resume_checkpoint_asset_hash"), "resume_checkpoint_asset_hash")
        except WorkerError as exc:
            raise WorkerError("CHECKPOINT_INVALID", str(exc)) from exc
        checkpoint = _load_json_asset(host, checkpoint_id, checkpoint_hash, limits=limits)
        if checkpoint.get("schema") != "checkpoint/v1" or not isinstance(checkpoint.get("state_asset_id"), str):
            raise WorkerError("CHECKPOINT_INVALID", "resume checkpoint has no state Asset")
        try:
            from plotpilot_plugin_sdk.verifier import verify_checkpoint
            verify_checkpoint(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"])
        except ImportError as exc:
            raise WorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable") from exc
        except Exception as exc:
            raise WorkerError("CHECKPOINT_INVALID", f"resume checkpoint failed public verification: {exc}") from exc
        if (
            checkpoint.get("job_id") != request["job_id"]
            or checkpoint.get("step_id") != request["step_id"]
            or checkpoint.get("source_attempt_id") != request["attempt_id"]
            or checkpoint.get("lease_epoch") != request["lease_epoch"]
            or checkpoint.get("run_snapshot_hash") != request["run_snapshot_hash"]
        ):
            raise WorkerError("CHECKPOINT_INVALID", "resume checkpoint is bound to another Core run")
        checkpoint_state_id = _strict_id(checkpoint["state_asset_id"], "checkpoint.state_asset_id")
        if state_id is not None and state_id != checkpoint_state_id:
            raise WorkerError("CHECKPOINT_INVALID", "resume state Asset differs from checkpoint")
        state_id = checkpoint_state_id
        checkpoint_state_hash = checkpoint.get("unit_set_hash")
        if checkpoint_state_hash is not None:
            try:
                checkpoint_state_hash = _strict_hash(checkpoint_state_hash, "checkpoint.unit_set_hash")
            except WorkerError as exc:
                raise WorkerError("CHECKPOINT_INVALID", str(exc)) from exc
            if state_hash is not None and state_hash != checkpoint_state_hash:
                raise WorkerError("CHECKPOINT_INVALID", "resume state hash differs from checkpoint")
            if state_hash is None:
                state_hash = checkpoint_state_hash
    if state_id is None:
        if state_hash is not None:
            raise WorkerError("CHECKPOINT_INVALID", "resume state hash has no state Asset")
        return 0, b"", 0, (), False
    try:
        state_id = _strict_id(state_id, "resume_state_asset_id")
        state_hash = _strict_hash(state_hash, "resume_state_asset_hash")
    except WorkerError as exc:
        raise WorkerError("CHECKPOINT_INVALID", str(exc)) from exc
    state = _load_json_asset(host, state_id, state_hash, limits=limits)
    expected_fields = {
        "schema", "source_asset_id", "source_asset_hash", "source_kind",
        "source_name", "page_size", "next_offset", "page_index",
        "prefix_asset_id", "prefix_asset_hash", "prefix_size", "request_hash",
        "capability_id", "run_snapshot_hash", "phase", "asset_eof",
    }
    if set(state) != expected_fields or state["schema"] != "source-import-checkpoint-state/v1":
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint state fields are not exact")
    try:
        _strict_id(state["source_asset_id"], "checkpoint.source_asset_id")
        _strict_id(state["prefix_asset_id"], "checkpoint.prefix_asset_id")
        _strict_hash(state["source_asset_hash"], "checkpoint.source_asset_hash")
        _strict_hash(state["prefix_asset_hash"], "checkpoint.prefix_asset_hash")
    except WorkerError as exc:
        raise WorkerError("CHECKPOINT_INVALID", str(exc)) from exc
    if (
        state["source_asset_id"] != request["source_asset_id"]
        or state["source_asset_hash"] != request["source_asset_hash"]
        or state["source_kind"] != request["source_kind"]
        or state["source_name"] != request.get("source_name")
        or state["capability_id"] != request["capability_id"]
        or state["run_snapshot_hash"] != request["run_snapshot_hash"]
    ):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint state is bound to another source Asset")
    phase = state["phase"]
    asset_eof = state["asset_eof"]
    if phase not in {"asset_read", "source_parse"} or not isinstance(asset_eof, bool):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint phase is invalid")
    if (phase == "source_parse") != asset_eof:
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint phase/EOF binding is inconsistent")
    if state["request_hash"] != _request_hash(_base_request(request)):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint state is bound to another request")
    values = tuple(state[name] for name in ("page_size", "next_offset", "page_index", "prefix_size"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint numeric state is invalid")
    page_size, next_offset, page_index, prefix_size = values
    if (
        page_size <= 0 or page_size > limits.max_page_bytes
        or next_offset != prefix_size or page_index > limits.max_pages
    ):
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint offset/page size is invalid")
    ensure_size("checkpoint prefix", prefix_size, limits.max_original_bytes)
    prefix = read_core_asset(
        host, state["prefix_asset_id"], state["prefix_asset_hash"],
        page_size=limits.max_page_bytes, limits=limits,
    ).data
    if len(prefix) != prefix_size or sha256_hex(prefix) != state["prefix_asset_hash"]:
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint prefix Asset is inconsistent")
    if asset_eof and sha256_hex(prefix) != request["source_asset_hash"]:
        raise WorkerError("CHECKPOINT_INVALID", "parse checkpoint does not contain the complete source Asset")
    prior_page_hashes = (
        (sha256_hex(b""),)
        if asset_eof and not prefix and page_index == 1
        else tuple(
            sha256_hex(prefix[start:start + page_size])
            for start in range(0, len(prefix), page_size)
        )
    )
    if len(prior_page_hashes) != page_index:
        raise WorkerError("CHECKPOINT_INVALID", "checkpoint page index does not match prefix pages")
    return next_offset, prefix, page_index, prior_page_hashes, asset_eof


def _upload(host: HostPort, request_hash: str, data: bytes, mime: str, suffix: str, *, limits: ImportLimits) -> str:
    expected_hash = sha256_hex(data)
    upload_id = _id("source-import-upload", request_hash, suffix)
    chunks = [b""] if not data else [
        data[offset:offset + limits.max_page_bytes]
        for offset in range(0, len(data), limits.max_page_bytes)
    ]
    accepted = 0
    asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _host_call(host, "host.asset.create/v1", {
            "operation_key": request_hash, "upload_id": upload_id, "offset": accepted,
            "mime": mime, "total_size": len(data), "expected_hash": expected_hash,
            "chunk_hash": sha256_hex(chunk), "base64_chunk": base64.b64encode(chunk).decode("ascii"),
            "final": final,
        })
        if response.get("upload_id") != upload_id:
            raise WorkerError("ASSET_CREATE_ERROR", "Host changed upload_id")
        returned = response.get("accepted_bytes")
        if isinstance(returned, bool) or not isinstance(returned, int) or returned != accepted + len(chunk):
            raise WorkerError("ASSET_CREATE_ERROR", "Host accepted_bytes is not contiguous")
        if response.get("completed") is not final:
            raise WorkerError("ASSET_CREATE_ERROR", "Host completed flag does not match final")
        raw_id = response.get("asset_id")
        if final:
            asset_id = _strict_id(raw_id, "created asset_id")
        elif raw_id is not None:
            raise WorkerError("ASSET_CREATE_ERROR", "nonfinal upload returned an Asset identity")
        accepted = returned
    status = _host_call(host, "host.asset.upload.status/v1", {
        "upload_id": upload_id, "expected_hash": expected_hash,
    })
    if (
        status.get("accepted_bytes") != len(data)
        or status.get("completed") is not True
        or status.get("asset_id") != asset_id
    ):
        raise WorkerError("ASSET_UPLOAD_ERROR", "Host upload status does not match completed Asset")
    return asset_id


def _best_effort_event(host: HostPort | None, request_hash: str, event_type: str, payload_asset_id: str | None, local_seq: int) -> None:
    if host is None or not hasattr(host, "call"):
        return
    try:
        response = _host_call(host, "host.job.event/v1", {
            "operation_key": request_hash, "event_type": event_type,
            "payload_asset_id": payload_asset_id, "local_seq": local_seq,
        })
        if response.get("accepted") is not True or not isinstance(response.get("job_event_seq"), int):
            raise WorkerError("HOST_CONTRACT_ERROR", "Host job event acknowledgement is invalid")
    except WorkerError as exc:
        if exc.code != "HOST_RPC_ERROR":
            raise


def _rebind(parsed: ParsedSource, asset_id: str) -> ParsedSource:
    from .contract import build_canonical_receipt, build_provisional_receipt
    provisional_bytes = parsed.provisional_text.encode("utf-8")
    provisional = build_provisional_receipt(
        raw_receipt=parsed.raw_receipt, decoder_receipt=parsed.decoder_receipt,
        provisional_asset_id=asset_id, provisional_text_hash=sha256_hex(provisional_bytes),
        provisional_byte_length=len(provisional_bytes),
        structure_evidence_hash=parsed.structure_evidence_hash, source_kind=parsed.source_kind,
    )
    canonical = build_canonical_receipt(
        raw_receipt=parsed.raw_receipt, decoder_receipt=parsed.decoder_receipt,
        provisional_receipt=provisional, canonical_asset_id=asset_id,
        canonical_text_hash=sha256_hex(provisional_bytes),
        canonical_byte_length=len(provisional_bytes),
        structure_evidence_hash=parsed.structure_evidence_hash,
    )
    return replace(parsed, provisional_receipt=provisional, canonical_receipt=canonical)


def _source_ref(parsed: ParsedSource) -> dict[str, object]:
    if parsed.source_asset_id is None:
        raise WorkerError("ASSET_READ_ERROR", "Core source Asset identity is required")
    return {
        "workspace_id": None, "source_type": "asset",
        "source_id": parsed.source_asset_id, "revision_or_hash": parsed.source_asset_hash,
    }


def _producer(request: Mapping[str, Any], capability_id: str, release_id: str) -> dict[str, object]:
    return {
        "plugin_id": "com.plotpilot.novelagent.source-import", "release_id": release_id,
        "capability_id": capability_id, "job_id": request["job_id"],
        "step_id": request["step_id"], "attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
    }


def _bundle(request: Mapping[str, Any], capability_id: str, release_id: str, contract_id: str, bundle_type: str, bundle_id: str, items: list[dict[str, object]]) -> dict[str, object]:
    bundle: dict[str, object] = {
        "schema": "result-bundle/v1", "contract_id": contract_id, "bundle_id": bundle_id,
        "bundle_type": bundle_type, "producer": _producer(request, capability_id, release_id),
        "input_snapshot_hash": request["run_snapshot_hash"], "items": items,
        "warnings": [], "partial": False, "provenance_receipt_id": request["provenance_receipt_id"],
        "skill_chain_result_refs": [],
    }
    try:
        from plotpilot_plugin_sdk.verifier import verify_result_bundle
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=request.get("target", {}).get("workspace_id") if isinstance(request.get("target"), Mapping) else None,
            snapshot_hash_value=request["run_snapshot_hash"],
        )
    except ImportError as exc:
        raise WorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable") from exc
    except Exception as exc:
        raise WorkerError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return bundle


def _error_payload(exc: Exception) -> dict[str, object]:
    if isinstance(exc, (WorkerError, SourceImportError)):
        return {"code": getattr(exc, "code", "IMPORT_FAILED"), "message": str(exc), "retryable": getattr(exc, "retryable", False)}
    if isinstance(exc, ContractError):
        return {"code": "INPUT_INVALID", "message": str(exc), "retryable": False}
    return {"code": "IMPORT_FAILED", "message": f"{type(exc).__name__}: {exc}", "retryable": False}


class SourceImportPlugin:
    """Deterministic worker with no filesystem, clipboard, DB, or network access."""

    def __init__(self, *, limits: ImportLimits = DEFAULT_LIMITS) -> None:
        limits.validate()
        self.limits = limits
        self.last_stage_response: dict[str, object] | None = None
        try:
            from .package_identity import load_runtime_identity
            identity = load_runtime_identity()
        except Exception as exc:
            raise WorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc
        self.package_hash = str(identity["package_hash"])
        self.release_id = str(identity["release_id"])

    def cancel(self, worker_run_id: str, host: HostPort | None = None) -> dict[str, object]:
        _strict_id(worker_run_id, "worker_run_id")
        return {"accepted": _OPERATIONS.cancel(worker_run_id), "worker_run_id": worker_run_id}

    def _validate(self, request: Mapping[str, Any], capability: str) -> dict[str, Any]:
        validate_request_common(request, capability=capability)
        return dict(request)

    @staticmethod
    def _cancel_predicate(request: Mapping[str, Any], operation_event: threading.Event) -> Callable[[], bool]:
        run_id = str(request["worker_run_id"])
        return lambda: _OPERATIONS.is_cancelled(run_id, operation_event)

    def _read(self, request: Mapping[str, Any], host: HostPort, operation_event: threading.Event) -> tuple[bytes, int, tuple[str, ...], int]:
        offset, prefix, prior_pages, prior_page_hashes, asset_eof = _resume_prefix(request, host, limits=self.limits)
        if asset_eof:
            return prefix, offset, prior_page_hashes, prior_pages
        try:
            read = read_core_asset(
                host, str(request["source_asset_id"]), str(request["source_asset_hash"]),
                page_size=int(request.get("page_size", self.limits.max_page_bytes)),
                limits=self.limits, start_offset=offset, prefix=prefix,
                cancel_check=self._cancel_predicate(request, operation_event),
            )
        except ImportCancelled as exc:
            raise ImportCancelled(
                exc.offset, prior_pages + exc.page_index, exc.prefix,
                phase="asset_read", asset_eof=False,
            ) from exc
        return read.data, offset, prior_page_hashes + read.page_hashes, prior_pages + read.page_index

    def _parse_source(
        self,
        request: Mapping[str, Any],
        raw: bytes,
        page_count: int,
        operation_event: threading.Event,
    ) -> ParsedSource:
        try:
            return parse_source(
                raw, source_kind=request["source_kind"], source_asset_id=request["source_asset_id"],
                source_asset_hash=request["source_asset_hash"], source_name=request.get("source_name"),
                limits=self.limits, encoding=request.get("encoding"),
                cancel_check=self._cancel_predicate(request, operation_event),
            )
        except SourceImportError as exc:
            if exc.code == "CANCELLED":
                raise ImportCancelled(
                    len(raw), page_count, raw,
                    phase="source_parse", asset_eof=True,
                ) from exc
            raise

    def _commit_checkpoint(
        self,
        request: Mapping[str, Any],
        host: HostPort,
        *,
        raw_prefix: bytes,
        next_offset: int,
        page_index: int,
        request_hash: str,
        phase: str,
        asset_eof: bool,
    ) -> dict[str, object] | None:
        checkpoint_id = request.get("checkpoint_id")
        if checkpoint_id is None and isinstance(request.get("checkpoint_ids"), list) and request["checkpoint_ids"]:
            checkpoint_id = request["checkpoint_ids"][0]
        if checkpoint_id is None:
            return None
        _strict_id(checkpoint_id, "checkpoint_id")
        prefix_hash = sha256_hex(raw_prefix)
        if phase not in {"asset_read", "source_parse"} or (phase == "source_parse") != asset_eof:
            raise WorkerError("CHECKPOINT_INVALID", "checkpoint phase/EOF binding is inconsistent")
        if asset_eof and (
            next_offset != len(raw_prefix)
            or prefix_hash != request["source_asset_hash"]
        ):
            raise WorkerError("CHECKPOINT_INVALID", "parse checkpoint must contain the complete source Asset")
        prefix_id = _upload(host, request_hash, raw_prefix, "application/octet-stream", "checkpoint-prefix", limits=self.limits)
        state = {
            "schema": "source-import-checkpoint-state/v1",
            "source_asset_id": request["source_asset_id"], "source_asset_hash": request["source_asset_hash"],
            "source_kind": request["source_kind"], "source_name": request.get("source_name"),
            "capability_id": request["capability_id"], "run_snapshot_hash": request["run_snapshot_hash"],
            "phase": phase, "asset_eof": asset_eof,
            "page_size": int(request.get("page_size", self.limits.max_page_bytes)),
            "next_offset": next_offset, "page_index": page_index,
            "prefix_asset_id": prefix_id, "prefix_asset_hash": prefix_hash,
            "prefix_size": len(raw_prefix), "request_hash": _request_hash(_base_request(request)),
        }
        state_bytes = canonical_bytes(state)
        state_id = _upload(host, request_hash, state_bytes, "application/json", "checkpoint-state", limits=self.limits)
        checkpoint = build_checkpoint(
            request, checkpoint_id=str(checkpoint_id), checkpoint_seq=max(1, page_index),
            completed_units=page_index, total_units=None, state_asset_id=state_id,
            unit_set_hash=sha256_hex(state_bytes),
        )
        checkpoint_bytes = canonical_bytes(checkpoint)
        checkpoint_asset_id = _upload(host, request_hash, checkpoint_bytes, "application/json", "checkpoint", limits=self.limits)
        response = _host_call(host, "host.checkpoint.commit/v1", {
            "operation_key": request_hash, "checkpoint_asset_id": checkpoint_asset_id,
        })
        if response.get("accepted") is not True or response.get("checkpoint_id") != checkpoint_id:
            raise WorkerError("CHECKPOINT_COMMIT_ERROR", "Core rejected checkpoint identity")
        return {
            "checkpoint": checkpoint, "checkpoint_asset_id": checkpoint_asset_id,
            "checkpoint_asset_hash": sha256_hex(checkpoint_bytes),
            "state_asset_id": state_id, "state_asset_hash": sha256_hex(state_bytes),
        }

    def _complete(self, request: Mapping[str, Any], host: HostPort, *, request_hash: str, outcome: str, result_asset_id: str | None, receipt: Mapping[str, object], stage_key: str | None, local_seq: int) -> None:
        if host is None or not hasattr(host, "call"):
            return
        response = _host_call(host, "host.job.complete/v1", {
            "operation_key": request_hash, "worker_run_id": request["worker_run_id"],
            "outcome": outcome, "result_bundle_asset_id": result_asset_id,
            "candidate_stage_operation_key": stage_key, "terminal_detail_asset_id": None,
            "local_seq": local_seq,
        })
        if response.get("accepted") is not True:
            raise WorkerError("HOST_CONTRACT_ERROR", "Core did not accept terminal result")
        for field in ("attempt_state", "step_state", "job_state"):
            if response.get(field) != outcome:
                raise WorkerError("HOST_CONTRACT_ERROR", f"Core terminal {field} mismatch")
        if response.get("provenance_receipt_id") != receipt["receipt_id"]:
            raise WorkerError("HOST_CONTRACT_ERROR", "Core terminal receipt identity mismatch")

    def _envelope(self, schema: str, status: str, result: Mapping[str, object] | None, receipt: Mapping[str, object], error: Mapping[str, object] | None, checkpoint: Mapping[str, object] | None, read: Mapping[str, object] | None) -> dict[str, object]:
        return {
            "schema": schema, "status": status, "result": result,
            "conditional_result": None, "receipt": receipt, "error": error,
            "checkpoint": checkpoint, "read": read,
        }

    @staticmethod
    def _has_core_identity(request: object) -> bool:
        if not isinstance(request, Mapping):
            return False
        try:
            for field in ("worker_run_id", "job_id", "step_id", "attempt_id", "source_asset_id", "provenance_receipt_id"):
                _strict_id(request[field], field)
            _strict_hash(request["run_snapshot_hash"], "run_snapshot_hash")
            if isinstance(request["lease_epoch"], bool) or not isinstance(request["lease_epoch"], int) or request["lease_epoch"] < 1:
                return False
        except (KeyError, WorkerError):
            return False
        return True

    def _failure_receipt(self, request: Mapping[str, Any], capability: str) -> dict[str, Any]:
        receipt_request = dict(request)
        receipt_request["created_at"] = "2026-08-28T00:00:00Z"
        return build_provenance_receipt(
            receipt_request, capability_id=capability, release_id=self.release_id,
            package_hash=self.package_hash, bundle_id=None, bundle_hash=None, staged_items=[],
        )

    @staticmethod
    def _failure_request_hash(request: object) -> str:
        if isinstance(request, Mapping):
            try:
                return _request_hash(_base_request(request))
            except Exception:
                try:
                    body = json.dumps(dict(request), ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=repr).encode("utf-8")
                except Exception:
                    body = repr(request).encode("utf-8", errors="backslashreplace")
                return hashlib.sha256(body).hexdigest()
        return hashlib.sha256(repr(request).encode("utf-8", errors="backslashreplace")).hexdigest()

    def _failed_envelope(self, request: Mapping[str, Any], host: HostPort, *, capability: str, result_schema: str, exc: Exception, force_input: bool = False) -> dict[str, object]:
        if not self._has_core_identity(request):
            raise WorkerError("INPUT_INVALID", str(exc)) from exc
        receipt = self._failure_receipt(request, capability)
        request_hash = self._failure_request_hash(request)
        try:
            self._complete(request, host, request_hash=request_hash, outcome="failed", result_asset_id=None, receipt=receipt, stage_key=None, local_seq=2)
        except Exception:
            pass
        error = _error_payload(exc)
        if force_input:
            error = {"code": "INPUT_INVALID", "message": str(exc), "retryable": False}
        return self._envelope(result_schema, "failed", None, receipt, error, None, None)

    def _invoke(self, request: Mapping[str, Any], host: HostPort, *, capability: str, result_schema: str, handler: Callable[[dict[str, Any], HostPort, str, threading.Event], dict[str, object]]) -> dict[str, object]:
        _require_sdk()
        try:
            normalized = self._validate(request, capability)
            request_hash = _request_hash(_base_request(normalized))
        except Exception as exc:
            return self._failed_envelope(request, host, capability=capability, result_schema=result_schema, exc=exc, force_input=True)
        try:
            operation_event = _OPERATIONS.begin(str(normalized["worker_run_id"]))
        except Exception as exc:
            return self._failed_envelope(normalized, host, capability=capability, result_schema=result_schema, exc=exc)
        try:
            return handler(normalized, host, request_hash, operation_event)
        except Exception as exc:
            return self._failed_envelope(normalized, host, capability=capability, result_schema=result_schema, exc=exc)
        finally:
            _OPERATIONS.finish(str(normalized["worker_run_id"]), operation_event)

    def _validate_stage_response(self, response: Mapping[str, object], items: list[dict[str, object]]) -> list[str]:
        if response.get("accepted") is not True:
            raise WorkerError("CANDIDATE_STAGE_ERROR", "Core did not accept Candidate stage")
        rows = response.get("staged_items")
        if not isinstance(rows, list) or not rows:
            raise WorkerError("CANDIDATE_STAGE_ERROR", "Core returned no accepted Candidate stage rows")
        expected = [str(item["item_id"]) for item in items]
        row_ids: list[str] = []
        row_fields = {"item_id", "candidate_id", "stage_status", "publication_eligibility"}
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != row_fields:
                raise WorkerError("CANDIDATE_STAGE_ERROR", "Core stage row is not the exact closed object")
            item_id = _strict_id(row["item_id"], "stage.item_id")
            if item_id in row_ids:
                raise WorkerError("CANDIDATE_STAGE_ERROR", "Core stage rows contain duplicate item_id")
            row_ids.append(item_id)
            candidate_id = row["candidate_id"]
            if candidate_id is not None:
                _strict_id(candidate_id, "stage.candidate_id")
            if row["stage_status"] not in {"created", "existing"}:
                raise WorkerError("CANDIDATE_STAGE_ERROR", "Core stage row has an invalid stage_status")
            if row["publication_eligibility"] not in {"eligible", "review_only", "none"}:
                raise WorkerError("CANDIDATE_STAGE_ERROR", "Core stage row has an invalid publication_eligibility")
        if len(row_ids) != len(expected) or set(row_ids) != set(expected):
            raise WorkerError("CANDIDATE_STAGE_ERROR", "Core stage rows do not correspond one-to-one to Candidate items")
        return expected

    def inspect(self, request: Mapping[str, Any], host: HostPort) -> dict[str, object]:
        return self._invoke(
            request, host, capability=INSPECT_CAPABILITY,
            result_schema="source.import.inspect-result/v1", handler=self._inspect_valid,
        )

    def _inspect_valid(self, normalized: dict[str, Any], host: HostPort, request_hash: str, operation_event: threading.Event) -> dict[str, object]:
        _best_effort_event(host, request_hash, "source-import.started", None, 1)
        try:
            raw, _offset, page_hashes, page_count = self._read(normalized, host, operation_event)
            parsed = self._parse_source(normalized, raw, page_count, operation_event)
            canonical_asset_id = _upload(host, request_hash, parsed.canonical_bytes, "text/plain; charset=utf-8", "canonical", limits=self.limits)
            parsed = _rebind(parsed, canonical_asset_id)
            inspection = {
                "schema": "source-import-inspection/v1", "capability_id": INSPECT_CAPABILITY,
                "source": parsed.as_dict(), "read": {
                    "page_size": int(normalized.get("page_size", self.limits.max_page_bytes)),
                    "page_count": page_count, "page_hashes": list(page_hashes), "eof": True,
                },
            }
            inspection_bytes = canonical_bytes(inspection)
            inspection_asset_id = _upload(host, request_hash, inspection_bytes, "application/json", "inspection", limits=self.limits)
            source_ref = _source_ref(parsed)
            bundle_id = _id("source-import-inspect-bundle", request_hash)
            items = [
                {
                    "schema": "artifact-item/v1", "item_id": _id("source-import-canonical", request_hash),
                    "artifact_kind": "source-canonical-text", "payload_asset_id": canonical_asset_id,
                    "payload_hash": sha256_hex(parsed.canonical_bytes), "mime": "text/plain; charset=utf-8",
                    "source_refs": [source_ref], "status": "complete",
                },
                {
                    "schema": "artifact-item/v1", "item_id": _id("source-import-inspection", request_hash),
                    "artifact_kind": "source-import-inspection", "payload_asset_id": inspection_asset_id,
                    "payload_hash": sha256_hex(inspection_bytes), "mime": "application/json",
                    "source_refs": [source_ref], "status": "complete",
                },
            ]
            bundle = _bundle(normalized, INSPECT_CAPABILITY, self.release_id, "artifact-bundle/v1", "artifact", bundle_id, items)
            receipt = build_provenance_receipt(
                normalized, capability_id=INSPECT_CAPABILITY, release_id=self.release_id,
                package_hash=self.package_hash, bundle_id=bundle_id,
                bundle_hash=hash_jcs("result-bundle/v1", bundle),
            )
            bundle_asset_id = _upload(host, request_hash, canonical_bytes(bundle), "application/json", "result", limits=self.limits)
            self._complete(normalized, host, request_hash=request_hash, outcome="succeeded", result_asset_id=bundle_asset_id, receipt=receipt, stage_key=None, local_seq=2)
            return self._envelope(
                "source.import.inspect-result/v1", "succeeded", bundle, receipt, None, None,
                {"page_count": page_count, "page_hashes": list(page_hashes), "next_offset": None},
            )
        except ImportCancelled as exc:
            checkpoint = self._commit_checkpoint(
                normalized, host, raw_prefix=exc.prefix, next_offset=exc.offset,
                page_index=exc.page_index, request_hash=request_hash,
                phase=exc.phase, asset_eof=exc.asset_eof,
            )
            receipt = build_provenance_receipt(normalized, capability_id=INSPECT_CAPABILITY, release_id=self.release_id, package_hash=self.package_hash, bundle_id=None, bundle_hash=None)
            self._complete(normalized, host, request_hash=request_hash, outcome="cancelled", result_asset_id=None, receipt=receipt, stage_key=None, local_seq=2)
            return self._envelope(
                "source.import.inspect-result/v1", "cancelled", None, receipt,
                {"code": "CANCELLED", "message": "source import cancelled", "retryable": False},
                checkpoint, {"page_count": exc.page_index, "page_hashes": [], "next_offset": exc.offset},
            )
        except Exception as exc:
            error = _error_payload(exc)
            receipt = build_provenance_receipt(normalized, capability_id=INSPECT_CAPABILITY, release_id=self.release_id, package_hash=self.package_hash, bundle_id=None, bundle_hash=None)
            try:
                self._complete(normalized, host, request_hash=request_hash, outcome="failed", result_asset_id=None, receipt=receipt, stage_key=None, local_seq=2)
            except Exception:
                pass
            return self._envelope("source.import.inspect-result/v1", "failed", None, receipt, error, None, None)

    def parse(self, request: Mapping[str, Any], host: HostPort) -> dict[str, object]:
        return self._invoke(
            request, host, capability=PARSE_CAPABILITY,
            result_schema="source.import.parse-result/v1", handler=self._parse_valid,
        )

    def _parse_valid(self, normalized: dict[str, Any], host: HostPort, request_hash: str, operation_event: threading.Event) -> dict[str, object]:
        _best_effort_event(host, request_hash, "source-import.started", None, 1)
        try:
            raw, _offset, page_hashes, page_count = self._read(normalized, host, operation_event)
            parsed = self._parse_source(normalized, raw, page_count, operation_event)
            canonical_asset_id = _upload(host, request_hash, parsed.canonical_bytes, "text/plain; charset=utf-8", "canonical", limits=self.limits)
            parsed = _rebind(parsed, canonical_asset_id)
            structure_bytes = canonical_bytes(parsed.structure_evidence)
            structure_asset_id = _upload(host, request_hash, structure_bytes, "application/json", "structure", limits=self.limits)
            source_ref = _source_ref(parsed)
            target = normalized["target"]
            if target["entity_kind"] == "document":
                target_rows = [(target, normalized["base"], "document", "core/document-text/v1", canonical_asset_id)]
                if normalized.get("structure_target") is not None:
                    target_rows.append((
                        normalized["structure_target"], normalized.get("structure_base", normalized["base"]),
                        "node_structure", "core/node-structure/v1", structure_asset_id,
                    ))
            elif target["entity_kind"] == "node_structure":
                target_rows = [(target, normalized["base"], "node_structure", "core/node-structure/v1", structure_asset_id)]
            else:
                raise WorkerError("TARGET_UNSUPPORTED", "source import targets only document or node_structure")
            items: list[dict[str, object]] = []
            parent_id: str | None = None
            for index, (candidate_target, candidate_base, item_kind, payload_schema, payload_id) in enumerate(target_rows):
                item_id = _id("source-import-candidate", request_hash, str(index))
                payload_hash = sha256_hex(parsed.canonical_bytes if item_kind == "document" else structure_bytes)
                item: dict[str, object] = {
                    "schema": "candidate-item/v1", "item_id": item_id, "item_kind": item_kind,
                    "target": dict(candidate_target),
                    "mutation": {
                        "mode": "replace" if item_kind == "document" else "structure_patch",
                        "payload_schema": payload_schema, "payload_hash": payload_hash,
                    },
                    "payload_asset_id": payload_id, "base": dict(candidate_base),
                    "write_set": [{
                        "workspace_id": candidate_target["workspace_id"], "entity_kind": candidate_target["entity_kind"],
                        "entity_id": candidate_target["entity_id"], "revision_id": candidate_base["revision_id"],
                        "content_hash": candidate_base["content_hash"],
                    }],
                    "parent_candidate_ids": [parent_id] if parent_id else [],
                    "source_refs": [source_ref], "status": "complete",
                }
                items.append(item)
                if parent_id is None:
                    parent_id = item_id
            bundle_id = _id("source-import-parse-bundle", request_hash)
            bundle = _bundle(normalized, PARSE_CAPABILITY, self.release_id, "candidate-batch/v1", "candidate_batch", bundle_id, items)
            bundle_asset_id = _upload(host, request_hash, canonical_bytes(bundle), "application/json", "result", limits=self.limits)
            stage_key = _id("source-import-stage", request_hash)
            stage_response = _host_call(host, "host.candidate.stage/v1", {
                "operation_key": stage_key, "result_bundle_asset_id": bundle_asset_id,
                "input_snapshot_hash": normalized["run_snapshot_hash"],
            })
            staged_item_ids = self._validate_stage_response(stage_response, items)
            self.last_stage_response = deepcopy(dict(stage_response))
            receipt = build_provenance_receipt(
                normalized, capability_id=PARSE_CAPABILITY, release_id=self.release_id,
                package_hash=self.package_hash, bundle_id=bundle_id,
                bundle_hash=hash_jcs("result-bundle/v1", bundle),
                staged_items=staged_item_ids,
            )
            self._complete(normalized, host, request_hash=request_hash, outcome="succeeded", result_asset_id=bundle_asset_id, receipt=receipt, stage_key=stage_key, local_seq=2)
            return self._envelope(
                "source.import.parse-result/v1", "succeeded", bundle, receipt, None, None,
                {"page_count": page_count, "page_hashes": list(page_hashes), "next_offset": None},
            )
        except ImportCancelled as exc:
            checkpoint = self._commit_checkpoint(
                normalized, host, raw_prefix=exc.prefix, next_offset=exc.offset,
                page_index=exc.page_index, request_hash=request_hash,
                phase=exc.phase, asset_eof=exc.asset_eof,
            )
            receipt = build_provenance_receipt(normalized, capability_id=PARSE_CAPABILITY, release_id=self.release_id, package_hash=self.package_hash, bundle_id=None, bundle_hash=None)
            self._complete(normalized, host, request_hash=request_hash, outcome="cancelled", result_asset_id=None, receipt=receipt, stage_key=None, local_seq=2)
            return self._envelope(
                "source.import.parse-result/v1", "cancelled", None, receipt,
                {"code": "CANCELLED", "message": "source import cancelled", "retryable": False},
                checkpoint, {"page_count": exc.page_index, "page_hashes": [], "next_offset": exc.offset},
            )
        except Exception as exc:
            error = _error_payload(exc)
            receipt = build_provenance_receipt(normalized, capability_id=PARSE_CAPABILITY, release_id=self.release_id, package_hash=self.package_hash, bundle_id=None, bundle_hash=None)
            try:
                self._complete(normalized, host, request_hash=request_hash, outcome="failed", result_asset_id=None, receipt=receipt, stage_key=None, local_seq=2)
            except Exception:
                pass
            return self._envelope("source.import.parse-result/v1", "failed", None, receipt, error, None, None)

    def run(self, request: Mapping[str, Any], host: HostPort) -> dict[str, object]:
        _require_sdk()
        if isinstance(request, Mapping) and request.get("capability_id") == INSPECT_CAPABILITY:
            return self.inspect(request, host)
        if isinstance(request, Mapping) and request.get("capability_id") == PARSE_CAPABILITY:
            return self.parse(request, host)
        if isinstance(request, Mapping) and request.get("schema") == "source.import.inspect-request/v1":
            return self.inspect(request, host)
        if isinstance(request, Mapping) and request.get("schema") == "source.import.parse-request/v1":
            return self.parse(request, host)
        raise WorkerError("INPUT_INVALID", "request does not identify a source import capability")


def capability_descriptor(capability_id: str | None = None) -> dict[str, object]:
    from .package_identity import load_runtime_identity
    identity = load_runtime_identity()
    descriptors = {
        INSPECT_CAPABILITY: {
            "schema": "capability-provider/v1", "capability_id": INSPECT_CAPABILITY,
            "provider": {"plugin_id": "com.plotpilot.novelagent.source-import", "release_id": identity["release_id"]},
            "input_schema": "source.import.inspect-request/v1", "output_schema": "source.import.inspect-result/v1",
            "result_contract": "artifact-bundle/v1", "supports": ["run", "cancel"],
            "deterministic": True, "accepted_data_formats": [],
        },
        PARSE_CAPABILITY: {
            "schema": "capability-provider/v1", "capability_id": PARSE_CAPABILITY,
            "provider": {"plugin_id": "com.plotpilot.novelagent.source-import", "release_id": identity["release_id"]},
            "input_schema": "source.import.parse-request/v1", "output_schema": "source.import.parse-result/v1",
            "result_contract": "candidate-batch/v1", "supports": ["run", "resume", "cancel"],
            "deterministic": True, "accepted_data_formats": [],
        },
    }
    if capability_id is None:
        return descriptors[INSPECT_CAPABILITY]
    if capability_id not in descriptors:
        raise WorkerError("CAPABILITY_UNKNOWN", "unknown source import capability")
    return descriptors[capability_id]


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> dict[str, object]:
    if request is None:
        return capability_descriptor()
    if host is None:
        raise WorkerError("HOST_REQUIRED", "a Core HostPort is required")
    return SourceImportPlugin().run(request, host)
