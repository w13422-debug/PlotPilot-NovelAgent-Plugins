"""Host-bound runtime for exact qualified Style apply/review/refine operations."""
from __future__ import annotations

import base64
import hashlib
import re
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from .capability_spec import (
    CAPABILITIES,
    NEEDS,
    PLUGIN_ID,
    SPEC_BY_CAPABILITY,
    VERSION,
    descriptors,
)
from .contract import (
    StyleContractError,
    hash_json,
    merge_lexicons,
    sha256_bytes,
    validate_qualified_style,
)
from .package_identity import load_runtime_identity

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes_sdk
    from plotpilot_plugin_sdk.verifier import (
        validate_rpc_result as _validate_rpc_result_sdk,
    )
    from plotpilot_plugin_sdk.verifier import (
        verify_checkpoint as _verify_checkpoint_sdk,
    )
    from plotpilot_plugin_sdk.verifier import (
        verify_provenance_receipt as _verify_provenance_receipt_sdk,
    )
    from plotpilot_plugin_sdk.verifier import (
        verify_result_bundle as _verify_result_bundle_sdk,
    )
except ImportError as exc:  # pragma: no cover - isolated install gate
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _canonical_bytes_sdk = _validate_rpc_result_sdk = None
    _verify_checkpoint_sdk = _verify_provenance_receipt_sdk = None
    _verify_result_bundle_sdk = None
else:
    _SDK_IMPORT_ERROR = None


CAPABILITY_APPLY = "style.apply/v1"
CAPABILITY_REVIEW = "style.review/v1"
CAPABILITY_REFINE = "style.refine/v1"

_COMMON_FIELDS = frozenset({
    "schema", "capability_id", "operation_key", "operation", "job_id", "step_id",
    "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids",
    "provenance_receipt_id", "created_at", "total_units", "run_snapshot_hash",
    "workspace_id", "document_id", "source_revision_id", "source_asset_id",
    "source_content_hash", "style_pack_asset_id", "style_pack_asset_hash",
    "qualification_receipt_asset_id", "qualification_receipt_asset_hash",
    "model_profile_revision_id",
})
_APPLY_FIELDS = frozenset({"lexicon_assets", "candidate_count"})
_REVIEW_FIELDS = frozenset({"quality_rubric_asset_id", "quality_rubric_asset_hash"})
_REFINE_FIELDS = frozenset({
    "quality_rubric_asset_id", "quality_rubric_asset_hash", "review_result_asset_id",
    "review_result_asset_hash", "known_parent_candidate_ids", "candidate_count",
})
_RESUME_FIELDS = frozenset({
    "resume_checkpoint_asset_id", "resume_checkpoint_asset_hash",
    "resume_state_asset_id", "resume_state_asset_hash",
})
_LEXICON_BINDING_FIELDS = frozenset({
    "asset_id", "asset_hash", "data_plugin_id", "data_release_id", "bundle_hash", "order",
})
_RAW_AUTHORITY_FIELDS = frozenset({"text", "content", "path", "file_path", "url", "database", "router"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$")
_PAGE_SIZE = 8_388_608
_MAX_PAGES = 4096
_ZERO_HASH = "0" * 64


class StyleRuntimeWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TerminalContractError(StyleRuntimeWorkerError):
    pass


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass
class _RunContext:
    request_hash: str
    binding_hash: str
    checkpoint_ids: tuple[str, ...]
    last_job_event_seq: int = 0
    last_checkpoint_seq: int = 0
    last_local_seq: int = 0
    terminal_dispatched: bool = False
    terminal_outcome: str | None = None


@dataclass
class _ActiveRun:
    worker_run_id: str
    binding_hash: str
    cancelled: threading.Event


class _OperationDispatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: _ActiveRun | None = None

    def begin(self, worker_run_id: str, binding_hash: str) -> _ActiveRun:
        with self._lock:
            if self._active is not None:
                raise StyleRuntimeWorkerError("WORKER_BUSY", "style-runtime already has an active operation", retryable=True)
            self._active = _ActiveRun(worker_run_id, binding_hash, threading.Event())
            return self._active

    def cancel(self, worker_run_id: str, binding_hash: str) -> bool:
        with self._lock:
            if self._active is None or self._active.worker_run_id != worker_run_id or self._active.binding_hash != binding_hash:
                return False
            self._active.cancelled.set()
            return True

    def finish(self, active: _ActiveRun) -> None:
        with self._lock:
            if self._active is active:
                self._active = None


_DISPATCHER = _OperationDispatcher()


def _require_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or any(value is None for value in (
        _canonical_bytes_sdk, _validate_rpc_result_sdk, _verify_checkpoint_sdk,
        _verify_provenance_receipt_sdk, _verify_result_bundle_sdk,
    )):
        raise StyleRuntimeWorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; style-runtime is fail-closed") from _SDK_IMPORT_ERROR


def _identity() -> tuple[str, str]:
    try:
        value = load_runtime_identity()
    except Exception as exc:
        raise StyleRuntimeWorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc
    return str(value["package_hash"]), str(value["release_id"])


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise StyleRuntimeWorkerError("INPUT_INVALID", f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise StyleRuntimeWorkerError("INPUT_INVALID", f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise StyleRuntimeWorkerError("INPUT_INVALID", f"{label} must be an integer in range")
    return value


def _json_bytes(value: Any) -> bytes:
    _require_sdk()
    assert _canonical_bytes_sdk is not None
    return _canonical_bytes_sdk(value)


def _strict_json(data: bytes, *, code: str = "ASSET_JSON_INVALID") -> Any:
    try:
        from plotpilot_plugin_sdk.canonical import parse_json_bytes
        return parse_json_bytes(data)
    except Exception as exc:
        raise StyleRuntimeWorkerError(code, str(exc)) from exc


def _derived_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256((prefix + "\n" + "\n".join(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:48]}"


def _host_call(host: HostPort | None, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    if host is None or not hasattr(host, "call"):
        raise StyleRuntimeWorkerError("HOST_REQUIRED", "Core HostPort.call is required")
    request_params = dict(params)
    try:
        response = host.call(method, request_params)
    except StyleRuntimeWorkerError:
        raise
    except Exception as exc:
        raise StyleRuntimeWorkerError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}", retryable=True) from exc
    if not isinstance(response, Mapping):
        raise StyleRuntimeWorkerError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    assert _validate_rpc_result_sdk is not None
    validation_response = dict(response)
    # The accepted model broker fixture returns the immutable response Asset
    # content hash alongside the closed RPC projection.  Bind and consume it,
    # while validating the standardized projection with the public SDK.
    if method == "host.model.invoke/v1" and "content_hash" in validation_response:
        _hash(validation_response.pop("content_hash"), "model response content_hash")
    try:
        _validate_rpc_result_sdk(method, validation_response, request={"method": method, "params": request_params})
    except Exception as exc:
        raise StyleRuntimeWorkerError("HOST_CONTRACT_ERROR", f"{method} result failed public RPC schema: {exc}") from exc
    return dict(response)


def _read_asset(host: HostPort | None, asset_id: str, expected_hash: str | None) -> bytes:
    _id(asset_id, "asset_id")
    if expected_hash is not None:
        _hash(expected_hash, "expected Asset hash")
    offset = 0
    chunks: list[bytes] = []
    for _ in range(_MAX_PAGES):
        response = _host_call(host, "host.asset.read/v1", {"asset_id": asset_id, "offset": offset, "length": _PAGE_SIZE})
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset page lacks base64_chunk")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset page base64 is invalid") from exc
        if response.get("content_hash") != sha256_bytes(chunk):
            raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset page hash mismatch")
        chunks.append(chunk)
        next_offset = response.get("next_offset")
        expected_next = offset + len(chunk)
        if next_offset is None:
            break
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset != expected_next or next_offset <= offset:
            raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset next_offset is not contiguous")
        offset = next_offset
    else:
        raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset exceeded bounded page count")
    data = b"".join(chunks)
    if expected_hash is not None and sha256_bytes(data) != expected_hash:
        raise StyleRuntimeWorkerError("ASSET_READ_ERROR", "Asset hash mismatch")
    return data


def _load_json_asset(host: HostPort | None, asset_id: str, expected_hash: str) -> Any:
    raw = _read_asset(host, asset_id, expected_hash)
    value = _strict_json(raw)
    # Keep the runtime acceptance set identical to manufacturing: frozen JSON
    # inputs must be exact canonical bytes, not merely semantically equivalent
    # objects with a caller-supplied hash.
    if sha256_bytes(_json_bytes(value)) != expected_hash:
        raise StyleRuntimeWorkerError("ASSET_JSON_INVALID", "JSON Asset is not canonical")
    return value


def _upload(host: HostPort | None, context: _RunContext, data: bytes, mime: str, suffix: str, *, operation_key: str | None = None) -> str:
    expected_hash = sha256_bytes(data)
    durable_operation_key = operation_key or context.request_hash
    upload_id = _derived_id("upload", context.binding_hash, suffix)
    chunks = [b""] if not data else [data[offset:offset + _PAGE_SIZE] for offset in range(0, len(data), _PAGE_SIZE)]
    accepted = 0
    asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _host_call(host, "host.asset.create/v1", {
            "operation_key": durable_operation_key, "upload_id": upload_id, "offset": accepted,
            "mime": mime, "total_size": len(data), "expected_hash": expected_hash,
            "chunk_hash": sha256_bytes(chunk), "base64_chunk": base64.b64encode(chunk).decode("ascii"), "final": final,
        })
        if response.get("upload_id") != upload_id or response.get("accepted_bytes") != accepted + len(chunk) or response.get("completed") is not final:
            raise StyleRuntimeWorkerError("ASSET_CREATE_ERROR", "Asset upload acknowledgement is not contiguous")
        raw_id = response.get("asset_id")
        if final:
            asset_id = _id(raw_id, "Host final asset_id")
        elif raw_id is not None:
            raise StyleRuntimeWorkerError("ASSET_CREATE_ERROR", "non-final upload returned Asset ID")
        accepted += len(chunk)
    status = _host_call(host, "host.asset.upload.status/v1", {"upload_id": upload_id, "expected_hash": expected_hash})
    if status.get("accepted_bytes") != len(data) or status.get("completed") is not True or status.get("asset_id") != asset_id:
        raise StyleRuntimeWorkerError("ASSET_UPLOAD_ERROR", "upload status differs from completed Asset")
    assert asset_id is not None
    return asset_id


def _record_event(context: _RunContext, response: Mapping[str, object], method: str) -> None:
    if response.get("accepted") is not True:
        raise StyleRuntimeWorkerError("HOST_REJECTED", f"Core rejected {method}")
    sequence = response.get("job_event_seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= context.last_job_event_seq:
        raise StyleRuntimeWorkerError("HOST_CONTRACT_ERROR", f"{method} job_event_seq is not strictly monotonic")
    context.last_job_event_seq = sequence


def _event(host: HostPort | None, context: _RunContext, event_type: str, payload_asset_id: str | None, local_seq: int) -> None:
    response = _host_call(host, "host.job.event/v1", {
        "operation_key": context.request_hash, "event_type": event_type,
        "payload_asset_id": payload_asset_id, "local_seq": local_seq,
    })
    _record_event(context, response, "host.job.event/v1")
    context.last_local_seq = local_seq


def _binding_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in request.items() if key not in _RESUME_FIELDS}
    result["operation"] = "run"
    return result


def _request_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-request/v1", dict(request))


def _binding_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-binding/v1", _binding_projection(request))


def _context(request: Mapping[str, Any], capability: str) -> tuple[dict[str, Any], _RunContext]:
    value = _validate_request(request, capability)
    return value, _RunContext(_request_hash(value, capability), _binding_hash(value, capability), tuple(value["checkpoint_ids"]))


def _capability_fields(capability: str) -> frozenset[str]:
    return {CAPABILITY_APPLY: _APPLY_FIELDS, CAPABILITY_REVIEW: _REVIEW_FIELDS, CAPABILITY_REFINE: _REFINE_FIELDS}[capability]


def _validate_request(request: Mapping[str, Any], capability: str) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise StyleRuntimeWorkerError("INPUT_INVALID", "request must be an object")
    value = dict(request)
    if any(field in value for field in _RAW_AUTHORITY_FIELDS):
        raise StyleRuntimeWorkerError("INPUT_INVALID", "raw content/path/url/database/router fields are forbidden")
    operation = value.get("operation")
    if operation not in SPEC_BY_CAPABILITY[capability].supports:
        raise StyleRuntimeWorkerError("INPUT_INVALID", f"operation {operation!r} is not declared")
    allowed = set(_COMMON_FIELDS | _capability_fields(capability))
    if operation == "resume":
        allowed.update(_RESUME_FIELDS)
    if set(value) != allowed:
        raise StyleRuntimeWorkerError("INPUT_INVALID", f"request fields are not closed: missing={sorted(allowed-set(value))}, extra={sorted(set(value)-allowed)}")
    spec = SPEC_BY_CAPABILITY[capability]
    if value["schema"] != spec.input_schema or value["capability_id"] != capability or value["operation_key"] != capability:
        raise StyleRuntimeWorkerError("INPUT_INVALID", "request schema/capability/operation_key mismatch")
    for field in (
        "job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id",
        "workspace_id", "document_id", "source_revision_id", "source_asset_id",
        "style_pack_asset_id", "qualification_receipt_asset_id", "model_profile_revision_id",
    ):
        _id(value[field], field)
    for field in ("run_snapshot_hash", "source_content_hash", "style_pack_asset_hash", "qualification_receipt_asset_hash"):
        _hash(value[field], field)
    _integer(value["lease_epoch"], "lease_epoch", 1)
    _integer(value["total_units"], "total_units", 1)
    if not isinstance(value["created_at"], str) or _TIME_RE.fullmatch(value["created_at"]) is None:
        raise StyleRuntimeWorkerError("INPUT_INVALID", "created_at must use frozen RFC3339 UTC form")
    checkpoints = value["checkpoint_ids"]
    if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) != len(set(checkpoints)):
        raise StyleRuntimeWorkerError("INPUT_INVALID", "checkpoint_ids must be non-empty unique")
    for item in checkpoints:
        _id(item, "checkpoint_id")
    if capability in {CAPABILITY_APPLY, CAPABILITY_REFINE}:
        _integer(value["candidate_count"], "candidate_count", 1, 8)
    if capability == CAPABILITY_APPLY:
        assets = value["lexicon_assets"]
        if not isinstance(assets, list):
            raise StyleRuntimeWorkerError("INPUT_INVALID", "lexicon_assets must be an array")
        orders: list[int] = []
        seen_releases: set[tuple[str, str]] = set()
        for index, raw in enumerate(assets):
            if not isinstance(raw, Mapping) or set(raw) != _LEXICON_BINDING_FIELDS:
                raise StyleRuntimeWorkerError("INPUT_INVALID", f"lexicon_assets[{index}] is not closed")
            for field in ("asset_id", "data_plugin_id"):
                _id(raw[field], field)
            for field in ("asset_hash", "data_release_id", "bundle_hash"):
                _hash(raw[field], field)
            orders.append(_integer(raw["order"], "lexicon order", 0))
            key = (str(raw["data_plugin_id"]), str(raw["data_release_id"]))
            if key in seen_releases:
                raise StyleRuntimeWorkerError("INPUT_INVALID", "lexicon releases must be unique")
            seen_releases.add(key)
        if orders != list(range(len(orders))):
            raise StyleRuntimeWorkerError("INPUT_INVALID", "lexicon order must be contiguous from zero")
    if capability in {CAPABILITY_REVIEW, CAPABILITY_REFINE}:
        _id(value["quality_rubric_asset_id"], "quality_rubric_asset_id")
        _hash(value["quality_rubric_asset_hash"], "quality_rubric_asset_hash")
    if capability == CAPABILITY_REFINE:
        _id(value["review_result_asset_id"], "review_result_asset_id")
        _hash(value["review_result_asset_hash"], "review_result_asset_hash")
        parents = value["known_parent_candidate_ids"]
        if not isinstance(parents, list) or len(parents) != len(set(parents)):
            raise StyleRuntimeWorkerError("INPUT_INVALID", "known_parent_candidate_ids must be unique array")
        for item in parents:
            _id(item, "known_parent_candidate_id")
    if operation == "resume":
        for field in ("resume_checkpoint_asset_id", "resume_state_asset_id"):
            _id(value[field], field)
        for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"):
            _hash(value[field], field)
    return value


def _load_bindings(value: Mapping[str, Any], host: HostPort | None) -> dict[str, Any]:
    source = _read_asset(host, value["source_asset_id"], value["source_content_hash"])
    try:
        source.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise StyleRuntimeWorkerError("SOURCE_INVALID", "source Asset must be strict UTF-8") from exc
    raw_pack = _load_json_asset(host, value["style_pack_asset_id"], value["style_pack_asset_hash"])
    raw_receipt = _load_json_asset(host, value["qualification_receipt_asset_id"], value["qualification_receipt_asset_hash"])
    if not isinstance(raw_pack, Mapping) or not isinstance(raw_receipt, Mapping):
        raise StyleRuntimeWorkerError("STYLE_NOT_ELIGIBLE", "Style pack and qualification receipt must be JSON objects")
    try:
        pack, receipt = validate_qualified_style(raw_pack, raw_receipt)
    except StyleContractError as exc:
        raise StyleRuntimeWorkerError("STYLE_NOT_ELIGIBLE", str(exc)) from exc
    if pack["target_cas"]["workspace_id"] != value["workspace_id"]:
        raise StyleRuntimeWorkerError("STYLE_NOT_ELIGIBLE", "Style target workspace mismatch")
    if pack["target_cas"]["entity_id"] != value["document_id"]:
        raise StyleRuntimeWorkerError("STYLE_NOT_ELIGIBLE", "Style target entity mismatch")
    if pack["target_cas"]["base_revision_id"] != value["source_revision_id"] or pack["target_cas"]["base_content_hash"] != value["source_content_hash"]:
        raise StyleRuntimeWorkerError("STYLE_NOT_ELIGIBLE", "Style target CAS does not match exact source")
    result: dict[str, Any] = {"source": source, "style_pack": pack, "qualification_receipt": receipt}
    if value["capability_id"] == CAPABILITY_APPLY:
        lexicons: list[Mapping[str, Any]] = []
        for binding in value["lexicon_assets"]:
            raw = _load_json_asset(host, binding["asset_id"], binding["asset_hash"])
            if not isinstance(raw, Mapping):
                raise StyleRuntimeWorkerError("LEXICON_INVALID", "lexicon Asset must contain an object")
            if raw.get("plugin_id") not in {None, binding["data_plugin_id"]} or raw.get("release_id") not in {None, binding["data_release_id"]}:
                raise StyleRuntimeWorkerError("LEXICON_INVALID", "lexicon release binding mismatch")
            lexicons.append(raw)
        try:
            result["merged_lexicon"] = merge_lexicons(lexicons)
        except StyleContractError as exc:
            raise StyleRuntimeWorkerError("LEXICON_INVALID", str(exc)) from exc
    if value["capability_id"] in {CAPABILITY_REVIEW, CAPABILITY_REFINE}:
        result["quality_rubric"] = _load_json_asset(host, value["quality_rubric_asset_id"], value["quality_rubric_asset_hash"])
    if value["capability_id"] == CAPABILITY_REFINE:
        review = _load_json_asset(host, value["review_result_asset_id"], value["review_result_asset_hash"])
        if not isinstance(review, Mapping) or review.get("schema") != "result-bundle/v1" or review.get("contract_id") != "diagnostic-bundle/v1":
            raise StyleRuntimeWorkerError("REVIEW_INVALID", "refine requires an exact diagnostic Result Bundle")
        # A two-field forged diagnostic is not sufficient authority for a
        # refinement.  Verify the complete public Bundle profile and every
        # producer/source identity before invoking the model or staging a
        # Candidate.
        try:
            assert _verify_result_bundle_sdk is not None
            _verify_result_bundle_sdk(review, snapshot_hash_value=value["run_snapshot_hash"])
        except Exception as exc:
            raise StyleRuntimeWorkerError("REVIEW_INVALID", f"review Result Bundle failed public verification: {exc}") from exc
        expected_producer = _producer(value, CAPABILITY_REVIEW, _identity()[1])
        if review.get("producer") != expected_producer or review.get("provenance_receipt_id") != value["provenance_receipt_id"]:
            raise StyleRuntimeWorkerError("REVIEW_INVALID", "review producer/release/provenance does not match exact run")
        items = review.get("items")
        if not isinstance(items, list) or not items:
            raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic items are missing")
        for item in items:
            if not isinstance(item, Mapping) or item.get("schema") != "diagnostic-item/v1" or item.get("status") != "complete":
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review contains a non-complete diagnostic")
            # A diagnostic without durable details is not review evidence: it
            # can be forged by copying only the Bundle envelope.  Re-read the
            # immutable details Asset and bind its model receipt to the
            # corresponding source reference below.
            details_id = item.get("details_asset_id")
            details_hash = item.get("details_hash")
            if not isinstance(details_id, str) or not isinstance(details_hash, str):
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic details Asset is missing")
            details = _load_json_asset(host, details_id, details_hash)
            if not isinstance(details, Mapping) or not isinstance(details.get("model_receipt_id"), str):
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic details are not model-bound")
            refs = item.get("source_refs")
            if not isinstance(refs, list) or len(refs) != 4:
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic source refs are missing")
            ref_map = {ref.get("source_type"): ref for ref in refs if isinstance(ref, Mapping)}
            if len(ref_map) != len(refs) or set(ref_map) != {"canonical_revision", "style_release", "qualification_receipt", "model_receipt"}:
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic source refs are incomplete")
            expected_refs = {
                "canonical_revision": (value["workspace_id"], value["document_id"], value["source_revision_id"]),
                "style_release": (None, value["style_pack_asset_id"], value["style_pack_asset_hash"]),
                "qualification_receipt": (None, value["qualification_receipt_asset_id"], value["qualification_receipt_asset_hash"]),
            }
            for source_type, (workspace_id, source_id, revision_or_hash) in expected_refs.items():
                ref = ref_map[source_type]
                if (ref.get("workspace_id"), ref.get("source_id"), ref.get("revision_or_hash")) != (workspace_id, source_id, revision_or_hash):
                    raise StyleRuntimeWorkerError("REVIEW_INVALID", f"review {source_type} provenance drifted")
            model_ref = ref_map["model_receipt"]
            if model_ref.get("workspace_id") is not None or not isinstance(model_ref.get("source_id"), str) or not model_ref.get("source_id") or model_ref.get("revision_or_hash") != value["model_profile_revision_id"]:
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review model receipt provenance drifted")
            if details.get("model_receipt_id") != model_ref.get("source_id"):
                raise StyleRuntimeWorkerError("REVIEW_INVALID", "review diagnostic details receipt drifted")
        result["review_result"] = review
    return result


def _invoke_model(host: HostPort | None, value: Mapping[str, Any], context: _RunContext, model_request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    data = _json_bytes(model_request)
    request_asset_id = _upload(host, context, data, "application/json", "model-request")
    response = _host_call(host, "host.model.invoke/v1", {
        "operation_key": context.request_hash + "-model",
        "invocation_id": _derived_id("invocation", context.request_hash),
        "invocation_key": _derived_id("model-key", context.binding_hash, value["capability_id"]),
        "model_profile_revision_id": value["model_profile_revision_id"],
        "request_asset_id": request_asset_id,
        "replay_policy": "manual_if_unknown",
    })
    if response.get("state") != "received" or response.get("response_asset_id") is None or response.get("uncertainty") is not None:
        raise StyleRuntimeWorkerError("MODEL_INVOKE_ERROR", "model invocation did not return a certain response", retryable=response.get("state") == "uncertain")
    receipt_id = _id(response.get("receipt_id"), "model receipt_id")
    response_asset_id = _id(response.get("response_asset_id"), "model response_asset_id")
    content_hash = response.get("content_hash")
    output = _strict_json(_read_asset(host, response_asset_id, _hash(content_hash, "model response content_hash") if content_hash is not None else None))
    if not isinstance(output, dict):
        raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "model response must be an object")
    return output, receipt_id


def _source_refs(value: Mapping[str, Any], receipt_id: str) -> list[dict[str, Any]]:
    return [
        {"workspace_id": value["workspace_id"], "source_type": "canonical_revision", "source_id": value["document_id"], "revision_or_hash": value["source_revision_id"]},
        {"workspace_id": None, "source_type": "style_release", "source_id": value["style_pack_asset_id"], "revision_or_hash": value["style_pack_asset_hash"]},
        {"workspace_id": None, "source_type": "qualification_receipt", "source_id": value["qualification_receipt_asset_id"], "revision_or_hash": value["qualification_receipt_asset_hash"]},
        {"workspace_id": None, "source_type": "model_receipt", "source_id": receipt_id, "revision_or_hash": value["model_profile_revision_id"]},
    ]


def _producer(value: Mapping[str, Any], capability: str, release_id: str) -> dict[str, Any]:
    return {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": capability,
            "job_id": value["job_id"], "step_id": value["step_id"],
            "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"]}


def _bundle(value: Mapping[str, Any], capability: str, release_id: str, items: list[dict[str, Any]], *, failed: bool = False) -> dict[str, Any]:
    spec = SPEC_BY_CAPABILITY[capability]
    contract_id = "diagnostic-bundle/v1" if failed else spec.result_contract
    bundle_type = "diagnostic" if failed else spec.bundle_type
    request_hash = _request_hash(_binding_projection(value), capability)
    result = {
        "schema": "result-bundle/v1", "contract_id": contract_id,
        "bundle_id": _derived_id("bundle", request_hash, contract_id), "bundle_type": bundle_type,
        "producer": _producer(value, capability, release_id),
        "input_snapshot_hash": value["run_snapshot_hash"], "items": items,
        "warnings": [], "partial": failed, "provenance_receipt_id": value["provenance_receipt_id"],
        "skill_chain_result_refs": [],
    }
    assert _verify_result_bundle_sdk is not None
    try:
        _verify_result_bundle_sdk(
            result,
            snapshot_workspace_id=value["workspace_id"] if contract_id == "candidate-batch/v1" else None,
            snapshot_hash_value=value["run_snapshot_hash"],
            known_parent_ids=set(value.get("known_parent_candidate_ids", [])),
            attempt_state="failed" if failed else None,
        )
    except Exception as exc:
        raise StyleRuntimeWorkerError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return result


def _candidate(value: Mapping[str, Any], context: _RunContext, text: str, ordinal: int, receipt_id: str, pack: Mapping[str, Any], qualification: Mapping[str, Any], host: HostPort | None) -> dict[str, Any]:
    payload = {
        "schema": "style-text-candidate/v1", "operation": value["capability_id"], "text": text,
        "style_pack_id": pack["style_pack_id"], "style_release_id": pack["style_release_id"],
        "style_payload_hash": pack["payload_hash"], "qualification_receipt_hash": qualification["receipt_hash"],
        "model_receipt_id": receipt_id,
    }
    data = _json_bytes(payload)
    payload_asset_id = _upload(host, context, data, "application/json", f"candidate-{ordinal}")
    payload_hash = sha256_bytes(data)
    item_id = _derived_id("candidate", context.binding_hash, str(ordinal), payload_hash)
    return {
        "schema": "candidate-item/v1", "item_id": item_id, "item_kind": "document",
        "target": {"workspace_id": value["workspace_id"], "entity_kind": "document", "entity_id": value["document_id"]},
        "mutation": {"mode": "replace", "payload_schema": "style-text-candidate/v1", "payload_hash": payload_hash},
        "payload_asset_id": payload_asset_id,
        "base": {"revision_id": value["source_revision_id"], "content_hash": value["source_content_hash"]},
        "write_set": [{"workspace_id": value["workspace_id"], "entity_kind": "document", "entity_id": value["document_id"], "revision_id": value["source_revision_id"], "content_hash": value["source_content_hash"]}],
        "parent_candidate_ids": list(value.get("known_parent_candidate_ids", [])),
        "source_refs": _source_refs(value, receipt_id), "status": "complete",
    }


def _diagnostic(value: Mapping[str, Any], context: _RunContext, ordinal: int, raw: Mapping[str, Any], receipt_id: str | None, host: HostPort | None, *, failed: bool = False) -> dict[str, Any]:
    severity = raw.get("severity", "error" if failed else "warning")
    if severity not in {"info", "warning", "error"}:
        raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "diagnostic severity is invalid")
    code = raw.get("code")
    message = raw.get("message")
    if not isinstance(code, str) or not code or not isinstance(message, str):
        raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "diagnostic code/message is invalid")
    details = dict(raw)
    if receipt_id is not None:
        details.update({"schema": "style-review-diagnostic/v1", "model_receipt_id": receipt_id})
    data = _json_bytes(details)
    details_asset_id = _upload(host, context, data, "application/json", f"diagnostic-{ordinal}") if host is not None else None
    return {
        "schema": "diagnostic-item/v1", "item_id": _derived_id("diagnostic", context.binding_hash, str(ordinal), code),
        "severity": severity, "code": code, "message": message,
        "details_asset_id": details_asset_id, "details_hash": sha256_bytes(data) if details_asset_id else None,
        "source_refs": _source_refs(value, receipt_id) if receipt_id is not None else [],
        "status": "failed" if failed else "complete",
    }


def _model_items(output: Mapping[str, Any], expected_schema: str) -> list[Mapping[str, Any]]:
    if set(output) != {"schema", "items"} or output.get("schema") != expected_schema or not isinstance(output.get("items"), list) or not output["items"]:
        raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "model output envelope is not closed")
    if any(not isinstance(item, Mapping) for item in output["items"]):
        raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "model output item must be an object")
    return list(output["items"])


def _stage(host: HostPort | None, value: Mapping[str, Any], context: _RunContext, bundle_asset_id: str, bundle_hash: str, items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    stage_key = _derived_id("candidate-stage", context.binding_hash, bundle_hash)
    response = _host_call(host, "host.candidate.stage/v1", {
        "operation_key": stage_key, "result_bundle_asset_id": bundle_asset_id,
        "input_snapshot_hash": value["run_snapshot_hash"],
    })
    if response.get("accepted") is not True:
        raise StyleRuntimeWorkerError("CANDIDATE_STAGE_ERROR", "Core rejected Candidate stage")
    rows = response.get("staged_items")
    expected = [item["item_id"] for item in items]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise StyleRuntimeWorkerError("CANDIDATE_STAGE_ERROR", "Candidate stage cardinality mismatch")
    observed: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "candidate_id", "stage_status", "publication_eligibility"}:
            raise StyleRuntimeWorkerError("CANDIDATE_STAGE_ERROR", "Candidate stage row is not closed")
        observed.append(_id(row["item_id"], "staged item_id"))
        _id(row["candidate_id"], "candidate_id")
    if observed != expected:
        raise StyleRuntimeWorkerError("CANDIDATE_STAGE_ERROR", "Candidate stage order mismatch")
    seq = response.get("job_event_seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq > context.last_job_event_seq:
        context.last_job_event_seq = seq
    return stage_key, observed


def _receipt(value: Mapping[str, Any], capability: str, package_hash: str, release_id: str, bundle: Mapping[str, Any] | None, staged: list[str], model_receipts: list[str]) -> dict[str, Any]:
    receipt = {
        "schema": "provenance-receipt/v1", "receipt_id": value["provenance_receipt_id"],
        "plugin_id": PLUGIN_ID, "release_id": release_id, "package_hash": package_hash,
        "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"],
        "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"],
        "run_snapshot_hash": value["run_snapshot_hash"],
        "bundle_id": None if bundle is None else bundle["bundle_id"],
        "bundle_hash": None if bundle is None else hash_json("result-bundle/v1", bundle),
        "parent_receipt_ids": [], "model_receipt_ids": list(model_receipts),
        "skill_chain_result_refs": [], "staged_items": list(staged), "created_at": value["created_at"],
    }
    receipt["receipt_hash"] = hash_json("provenance-receipt/v1", receipt)
    assert _verify_provenance_receipt_sdk is not None
    try:
        _verify_provenance_receipt_sdk(receipt)
    except Exception as exc:
        raise StyleRuntimeWorkerError("RECEIPT_CONTRACT_ERROR", str(exc)) from exc
    return receipt


def _checkpoint(host: HostPort | None, value: Mapping[str, Any], context: _RunContext, state_asset_id: str, state_hash: str) -> dict[str, Any]:
    if context.last_checkpoint_seq >= len(context.checkpoint_ids):
        raise StyleRuntimeWorkerError("CHECKPOINT_ERROR", "checkpoint budget exhausted")
    checkpoint = {
        "schema": "checkpoint/v1", "checkpoint_id": context.checkpoint_ids[context.last_checkpoint_seq],
        "checkpoint_seq": context.last_checkpoint_seq + 1, "job_id": value["job_id"], "step_id": value["step_id"],
        "source_attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"],
        "run_snapshot_hash": value["run_snapshot_hash"], "replay_policy": "checkpoint_resume",
        "completed_units": value["total_units"], "total_units": value["total_units"],
        "unit_set_hash": state_hash, "state_asset_id": state_asset_id, "created_at": value["created_at"],
    }
    checkpoint["checkpoint_hash"] = hash_json("checkpoint/v1", checkpoint)
    assert _verify_checkpoint_sdk is not None
    try:
        _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=value["run_snapshot_hash"], previous_seq=None)
    except Exception as exc:
        raise StyleRuntimeWorkerError("CHECKPOINT_CONTRACT_ERROR", str(exc)) from exc
    data = _json_bytes(checkpoint)
    checkpoint_asset_id = _upload(host, context, data, "application/json", "checkpoint")
    response = _host_call(host, "host.checkpoint.commit/v1", {"operation_key": context.request_hash + "-checkpoint", "checkpoint_asset_id": checkpoint_asset_id})
    _record_event(context, response, "host.checkpoint.commit/v1")
    if response.get("checkpoint_id") != checkpoint["checkpoint_id"]:
        raise StyleRuntimeWorkerError("CHECKPOINT_ERROR", "checkpoint acknowledgement mismatch")
    context.last_checkpoint_seq += 1
    return {"checkpoint": checkpoint, "checkpoint_asset_id": checkpoint_asset_id,
            "checkpoint_asset_hash": sha256_bytes(data), "state_asset_id": state_asset_id,
            "state_asset_hash": state_hash}


def _complete(host: HostPort | None, value: Mapping[str, Any], context: _RunContext, outcome: str, bundle_asset_id: str | None, stage_key: str | None, detail_asset_id: str | None, local_seq: int) -> None:
    if context.terminal_dispatched:
        raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED", "terminal completion may be dispatched at most once")
    context.terminal_dispatched = True
    context.terminal_outcome = outcome
    response = _host_call(host, "host.job.complete/v1", {
        "operation_key": context.request_hash, "worker_run_id": value["worker_run_id"],
        "outcome": outcome, "result_bundle_asset_id": bundle_asset_id,
        "candidate_stage_operation_key": stage_key, "terminal_detail_asset_id": detail_asset_id,
        "local_seq": local_seq,
    })
    _record_event(context, response, "host.job.complete/v1")
    expected = {"succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled"}[outcome]
    if response.get("attempt_state") != expected or response.get("provenance_receipt_id") != value["provenance_receipt_id"]:
        raise TerminalContractError("TERMINAL_CONTRACT_ERROR", "Host terminal acknowledgement mismatch")


_STATE_FIELDS = frozenset({
    "schema", "binding_hash", "plugin_id", "package_hash", "release_id", "capability_id",
    "job_id", "step_id", "attempt_id", "worker_run_id", "lease_epoch", "run_snapshot_hash",
    "style_release_id", "style_payload_hash", "qualification_receipt_hash",
    "result_bundle_asset_id", "result_bundle_hash", "model_receipt_ids", "checkpoint_id", "checkpoint_seq",
})


class StyleRuntimePlugin:
    plugin_id = PLUGIN_ID
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self.package_hash, self.release_id = _identity()
        self.last_receipt: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.last_stage_response: dict[str, Any] | None = None
        self.last_model_receipt_ids: list[str] = []

    def validate(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        capability = request.get("capability_id") if isinstance(request, Mapping) else None
        if capability not in CAPABILITIES:
            raise StyleRuntimeWorkerError("CAPABILITY_UNKNOWN", "unknown style-runtime capability")
        value = _validate_request(request, capability)
        bindings = _load_bindings(value, host)
        return {
            "valid": True, "capability_id": capability,
            "style_pack_id": bindings["style_pack"]["style_pack_id"],
            "style_release_id": bindings["style_pack"]["style_release_id"],
            "style_payload_hash": bindings["style_pack"]["payload_hash"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
        }

    def cancel(self, request: Mapping[str, Any]) -> dict[str, object]:
        capability = request.get("capability_id")
        if capability not in CAPABILITIES:
            raise StyleRuntimeWorkerError("CAPABILITY_UNKNOWN", "unknown style-runtime capability")
        value, context = _context(request, capability)
        return {"accepted": _DISPATCHER.cancel(value["worker_run_id"], context.binding_hash), "worker_run_id": value["worker_run_id"]}

    def apply(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_APPLY)
        bindings = _load_bindings(value, host)
        model_request = {
            "schema": "style.apply-model-request/v1", "source_asset_id": value["source_asset_id"],
            "source_content_hash": value["source_content_hash"], "style_pack": bindings["style_pack"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
            "merged_lexicon": bindings["merged_lexicon"], "candidate_count": value["candidate_count"],
            "output_schema": "style.apply-model-response/v1",
        }
        output, receipt_id = _invoke_model(host, value, context, model_request)
        self.last_model_receipt_ids = [receipt_id]
        items = _model_items(output, "style.apply-model-response/v1")
        if len(items) != value["candidate_count"]:
            raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "apply candidate count mismatch")
        candidates = []
        for index, item in enumerate(items):
            if set(item) != {"text"} or not isinstance(item["text"], str) or not item["text"]:
                raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "apply item must contain only non-empty text")
            candidates.append(_candidate(value, context, item["text"], index, receipt_id, bindings["style_pack"], bindings["qualification_receipt"], host))
        return _bundle(value, CAPABILITY_APPLY, self.release_id, candidates)

    def review(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_REVIEW)
        bindings = _load_bindings(value, host)
        model_request = {
            "schema": "style.review-model-request/v1", "source_asset_id": value["source_asset_id"],
            "source_content_hash": value["source_content_hash"], "style_pack": bindings["style_pack"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
            "quality_rubric": bindings["quality_rubric"], "output_schema": "style.review-model-response/v1",
        }
        output, receipt_id = _invoke_model(host, value, context, model_request)
        self.last_model_receipt_ids = [receipt_id]
        items = _model_items(output, "style.review-model-response/v1")
        diagnostics = []
        for index, item in enumerate(items):
            if set(item) != {"severity", "code", "message", "dimension", "score_0_100", "recommendation"}:
                raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "review diagnostic fields are not closed")
            _integer(item["score_0_100"], "score_0_100", 0, 100)
            if not isinstance(item["dimension"], str) or not item["dimension"] or not isinstance(item["recommendation"], str):
                raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "review diagnostic text fields are invalid")
            diagnostics.append(_diagnostic(value, context, index, item, receipt_id, host))
        return _bundle(value, CAPABILITY_REVIEW, self.release_id, diagnostics)

    def refine(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_REFINE)
        bindings = _load_bindings(value, host)
        model_request = {
            "schema": "style.refine-model-request/v1", "source_asset_id": value["source_asset_id"],
            "source_content_hash": value["source_content_hash"], "style_pack": bindings["style_pack"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
            "quality_rubric": bindings["quality_rubric"], "review_result": bindings["review_result"],
            "candidate_count": value["candidate_count"], "output_schema": "style.refine-model-response/v1",
        }
        output, receipt_id = _invoke_model(host, value, context, model_request)
        self.last_model_receipt_ids = [receipt_id]
        items = _model_items(output, "style.refine-model-response/v1")
        if len(items) != value["candidate_count"]:
            raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "refine candidate count mismatch")
        candidates = []
        for index, item in enumerate(items):
            if set(item) != {"text"} or not isinstance(item["text"], str) or not item["text"]:
                raise StyleRuntimeWorkerError("MODEL_OUTPUT_INVALID", "refine item must contain only non-empty text")
            candidates.append(_candidate(value, context, item["text"], index, receipt_id, bindings["style_pack"], bindings["qualification_receipt"], host))
        return _bundle(value, CAPABILITY_REFINE, self.release_id, candidates)

    def _finalize(self, value: Mapping[str, Any], capability: str, context: _RunContext, bundle: dict[str, Any], bindings: Mapping[str, Any], host: HostPort | None, active: _ActiveRun) -> dict[str, Any] | None:
        data = _json_bytes(bundle)
        bundle_hash = sha256_bytes(data)
        bundle_asset_id = _upload(host, context, data, "application/json", "result-bundle", operation_key=_derived_id("result-upload-operation", context.binding_hash))
        _event(host, context, "style-runtime.result", bundle_asset_id, 2)
        state = {
            "schema": "style-runtime-resume-state/v1", "binding_hash": context.binding_hash,
            "plugin_id": PLUGIN_ID, "package_hash": self.package_hash, "release_id": self.release_id,
            "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"],
            "attempt_id": value["attempt_id"], "worker_run_id": value["worker_run_id"], "lease_epoch": value["lease_epoch"],
            "run_snapshot_hash": value["run_snapshot_hash"], "style_release_id": bindings["style_pack"]["style_release_id"],
            "style_payload_hash": bindings["style_pack"]["payload_hash"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
            "result_bundle_asset_id": bundle_asset_id, "result_bundle_hash": bundle_hash,
            "model_receipt_ids": list(self.last_model_receipt_ids),
            "checkpoint_id": context.checkpoint_ids[context.last_checkpoint_seq], "checkpoint_seq": context.last_checkpoint_seq + 1,
        }
        state_data = _json_bytes(state)
        state_asset_id = _upload(host, context, state_data, "application/json", "resume-state")
        self.last_checkpoint = _checkpoint(host, value, context, state_asset_id, sha256_bytes(state_data))
        if active.cancelled.is_set():
            self.last_receipt = _receipt(value, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids)
            _complete(host, value, context, "cancelled", None, None, None, 3)
            return None
        stage_key: str | None = None
        staged: list[str] = []
        if bundle["contract_id"] == "candidate-batch/v1":
            stage_key, staged = _stage(host, value, context, bundle_asset_id, bundle_hash, bundle["items"])
        self.last_receipt = _receipt(value, capability, self.package_hash, self.release_id, bundle, staged, self.last_model_receipt_ids)
        _complete(host, value, context, "succeeded", bundle_asset_id, stage_key, None, 3)
        return bundle

    def _resume(self, value: Mapping[str, Any], capability: str, context: _RunContext, bindings: Mapping[str, Any], host: HostPort | None, active: _ActiveRun) -> dict[str, Any] | None:
        _event(host, context, "style-runtime.resumed", value["resume_state_asset_id"], 1)
        checkpoint = _load_json_asset(host, value["resume_checkpoint_asset_id"], value["resume_checkpoint_asset_hash"])
        try:
            assert _verify_checkpoint_sdk is not None
            _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=value["run_snapshot_hash"], previous_seq=None)
        except Exception as exc:
            raise StyleRuntimeWorkerError("RESUME_INVALID", f"checkpoint binding failed: {exc}") from exc
        if checkpoint.get("checkpoint_id") not in value["checkpoint_ids"]:
            raise StyleRuntimeWorkerError("RESUME_INVALID", "checkpoint is not declared by this request")
        state = _load_json_asset(host, value["resume_state_asset_id"], value["resume_state_asset_hash"])
        if not isinstance(state, Mapping) or set(state) != _STATE_FIELDS:
            raise StyleRuntimeWorkerError("RESUME_INVALID", "resume state is not closed")
        expected = {
            "schema": "style-runtime-resume-state/v1", "binding_hash": context.binding_hash,
            "plugin_id": PLUGIN_ID, "package_hash": self.package_hash, "release_id": self.release_id,
            "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"],
            "attempt_id": value["attempt_id"], "worker_run_id": value["worker_run_id"], "lease_epoch": value["lease_epoch"],
            "run_snapshot_hash": value["run_snapshot_hash"], "style_release_id": bindings["style_pack"]["style_release_id"],
            "style_payload_hash": bindings["style_pack"]["payload_hash"],
            "qualification_receipt_hash": bindings["qualification_receipt"]["receipt_hash"],
            "checkpoint_id": checkpoint["checkpoint_id"], "checkpoint_seq": checkpoint["checkpoint_seq"],
        }
        for field, expected_value in expected.items():
            if state.get(field) != expected_value:
                raise StyleRuntimeWorkerError("RESUME_INVALID", f"resume state {field} mismatch")
        if checkpoint.get("state_asset_id") != value["resume_state_asset_id"] or checkpoint.get("unit_set_hash") != value["resume_state_asset_hash"]:
            raise StyleRuntimeWorkerError("RESUME_INVALID", "checkpoint/state CAS mismatch")
        bundle_bytes = _read_asset(host, _id(state["result_bundle_asset_id"], "result_bundle_asset_id"), _hash(state["result_bundle_hash"], "result_bundle_hash"))
        bundle = _strict_json(bundle_bytes, code="RESUME_INVALID")
        try:
            assert _verify_result_bundle_sdk is not None
            _verify_result_bundle_sdk(bundle, snapshot_workspace_id=value["workspace_id"] if bundle.get("contract_id") == "candidate-batch/v1" else None,
                                      snapshot_hash_value=value["run_snapshot_hash"], known_parent_ids=set(value.get("known_parent_candidate_ids", [])))
        except Exception as exc:
            raise StyleRuntimeWorkerError("RESUME_INVALID", f"Result Bundle binding failed: {exc}") from exc
        if bundle.get("producer") != _producer(value, capability, self.release_id):
            raise StyleRuntimeWorkerError("RESUME_INVALID", "Result Bundle producer mismatch")
        model_receipts = state.get("model_receipt_ids")
        if not isinstance(model_receipts, list) or not model_receipts or len(model_receipts) != len(set(model_receipts)):
            raise StyleRuntimeWorkerError("RESUME_INVALID", "model receipt state is invalid")
        self.last_model_receipt_ids = list(model_receipts)
        if active.cancelled.is_set():
            self.last_receipt = _receipt(value, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids)
            _complete(host, value, context, "cancelled", None, None, None, 3)
            return None
        stage_key: str | None = None
        staged: list[str] = []
        if bundle["contract_id"] == "candidate-batch/v1":
            stage_key, staged = _stage(host, value, context, state["result_bundle_asset_id"], state["result_bundle_hash"], bundle["items"])
        self.last_receipt = _receipt(value, capability, self.package_hash, self.release_id, bundle, staged, self.last_model_receipt_ids)
        _complete(host, value, context, "succeeded", state["result_bundle_asset_id"], stage_key, None, 3)
        return dict(bundle)

    def _failure(self, value: Mapping[str, Any], capability: str, context: _RunContext | None, host: HostPort | None, error: BaseException) -> dict[str, Any] | None:
        if isinstance(error, TerminalContractError):
            raise error
        try:
            if context is None:
                for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id"):
                    _id(value.get(field), field)
                _integer(value.get("lease_epoch"), "lease_epoch", 1)
                _hash(value.get("run_snapshot_hash"), "run_snapshot_hash")
                context = _RunContext(hash_json(capability + "-failure-request/v1", dict(value)), _ZERO_HASH, tuple(value.get("checkpoint_ids", [])))
            code = getattr(error, "code", "INTERNAL_ERROR")
            message = str(error)[:1024]
            raw = {"schema": "style-runtime-failure/v1", "severity": "error", "code": code,
                   "message": message, "retryable": bool(getattr(error, "retryable", False))}
            item = _diagnostic(value, context, 0, raw, None, host, failed=True)
            bundle = _bundle(value, capability, self.release_id, [item], failed=True)
            bundle_asset_id = None
            detail_asset_id = item["details_asset_id"]
            if host is not None:
                bundle_asset_id = _upload(host, context, _json_bytes(bundle), "application/json", "failure-bundle")
                self.last_receipt = _receipt(value, capability, self.package_hash, self.release_id, bundle, [], self.last_model_receipt_ids)
                _event(host, context, "style-runtime.failed", detail_asset_id, max(1, context.last_local_seq + 1))
                _complete(host, value, context, "failed", bundle_asset_id, None, detail_asset_id, context.last_local_seq + 1)
            return bundle
        except TerminalContractError:
            raise
        except Exception as terminal_error:
            raise StyleRuntimeWorkerError("FAILURE_TERMINAL_ERROR", str(terminal_error)) from terminal_error

    def run(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any] | None:
        value: Mapping[str, Any] = request if isinstance(request, Mapping) else {}
        capability: str | None = None
        context: _RunContext | None = None
        active: _ActiveRun | None = None
        self.last_model_receipt_ids = []
        try:
            if not isinstance(request, Mapping):
                raise StyleRuntimeWorkerError("INPUT_INVALID", "request must be an object")
            value = dict(request)
            if "request_asset_id" in value:
                if set(value) != {"request_asset_id", "request_asset_hash"}:
                    raise StyleRuntimeWorkerError("INPUT_INVALID", "request Asset envelope is closed")
                loaded = _load_json_asset(host, _id(value["request_asset_id"], "request_asset_id"), _hash(value["request_asset_hash"], "request_asset_hash"))
                if not isinstance(loaded, Mapping):
                    raise StyleRuntimeWorkerError("INPUT_INVALID", "request Asset must contain an object")
                value = dict(loaded)
            capability = value.get("capability_id")
            if capability in (None, ""):
                capability = {spec.input_schema: spec.capability_id for spec in SPEC_BY_CAPABILITY.values()}.get(value.get("schema"))
            if capability not in CAPABILITIES:
                raise StyleRuntimeWorkerError("CAPABILITY_UNKNOWN", "unknown style-runtime capability")
            normalized, context = _context(value, capability)
            value = normalized
            if value["operation"] == "cancel":
                return self.cancel(value)
            active = _DISPATCHER.begin(value["worker_run_id"], context.binding_hash)
            bindings = _load_bindings(value, host)
            if value["operation"] == "resume":
                return self._resume(value, capability, context, bindings, host, active)
            _event(host, context, "style-runtime.started", None, 1)
            if capability == CAPABILITY_APPLY:
                bundle = self.apply(value, host)
            elif capability == CAPABILITY_REVIEW:
                bundle = self.review(value, host)
            else:
                bundle = self.refine(value, host)
            return self._finalize(value, capability, context, bundle, bindings, host, active)
        except TerminalContractError:
            raise
        except StyleRuntimeWorkerError as exc:
            if capability in CAPABILITIES:
                return self._failure(value, capability, context, host, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - all unexpected failures become a legal diagnostic Result
            if capability in CAPABILITIES:
                return self._failure(value, capability, context, host, StyleRuntimeWorkerError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"))
            return None
        finally:
            if active is not None:
                _DISPATCHER.finish(active)


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    capability = capability_id or CAPABILITY_APPLY
    if capability not in CAPABILITIES:
        raise StyleRuntimeWorkerError("CAPABILITY_UNKNOWN", "unknown style-runtime capability")
    _, release_id = _identity()
    return SPEC_BY_CAPABILITY[capability].descriptor(release_id)


PACKAGE_HASH, RELEASE_ID = _identity()
DESCRIPTORS = descriptors(RELEASE_ID)
_RUNTIME = StyleRuntimePlugin()


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> dict[str, Any] | None:
    if request is None:
        return capability_descriptor()
    return _RUNTIME.run(request, host)


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_APPLY",
    "CAPABILITY_REFINE",
    "CAPABILITY_REVIEW",
    "DESCRIPTORS",
    "NEEDS",
    "PACKAGE_HASH",
    "PLUGIN_ID",
    "RELEASE_ID",
    "VERSION",
    "HostPort",
    "StyleRuntimePlugin",
    "StyleRuntimeWorkerError",
    "capability_descriptor",
    "main",
]
