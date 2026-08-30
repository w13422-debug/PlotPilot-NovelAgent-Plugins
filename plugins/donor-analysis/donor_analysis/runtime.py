"""Host-bound worker for the four frozen donor-analysis capabilities."""
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
    ATOM_FIELDS, ATOM_KINDS, CLAIM_FIELDS, EVIDENCE_FIELDS, HASH_RE, DonorContractError,
    bind_current_accepted_atoms, build_atom_provenance, canonical_json_bytes,
    hash_json, sha256_text, validate_atom_payload, validate_claim_input,
    validate_claim_payload, validate_evidence_span, validate_nodes,
    validate_rereview_diagnostics, validate_rereview_records,
)
from .capability_spec import (
    CAPABILITIES, NEEDS, PLUGIN_ID, SPEC_BY_CAPABILITY, VERSION, descriptors,
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
except ImportError as exc:  # pragma: no cover - isolated install gate
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _canonical_bytes_sdk = _validate_rpc_result_sdk = None
    _verify_checkpoint_sdk = _verify_provenance_receipt_sdk = None
    _verify_result_bundle_sdk = None
else:
    _SDK_IMPORT_ERROR = None


CAPABILITY_ATOM_EXTRACT = "analysis.book.atom.extract/v1"
CAPABILITY_ATOM_MANUAL = "analysis.book.atom.manual/v1"
CAPABILITY_CLAIM_GENERATE = "analysis.book.claim.generate/v1"
CAPABILITY_REREVIEW = "analysis.book.rereview/v1"
_COMMON_FIELDS = frozenset({
    "schema", "capability_id", "operation_key", "operation", "job_id", "step_id",
    "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids",
    "provenance_receipt_id", "created_at", "total_units", "run_snapshot_hash",
    "workspace_id", "document_id",
})
_SOURCE_FIELDS = frozenset({
    "source_revision_id", "canonical_asset_id", "canonical_text_hash", "nodes",
    "taxonomy_asset_id", "taxonomy_asset_hash", "model_profile_revision_id",
})
_MANUAL_FIELDS = frozenset({
    "source_revision_id", "canonical_asset_id", "canonical_text_hash", "nodes",
    "taxonomy_asset_id", "taxonomy_asset_hash", "manual_annotation_asset_id",
    "manual_annotation_asset_hash", "actor_id", "atom_payload",
})
_CLAIM_FIELDS = frozenset({
    "source_revision_id", "canonical_asset_id", "canonical_text_hash", "nodes",
    "parameters_asset_id", "parameters_asset_hash",
    "snapshot_parameters_asset_id", "snapshot_asset_hashes",
    "accepted_atoms", "model_profile_revision_id",
})
_REREVIEW_FIELDS = frozenset({
    "source_revision_id", "canonical_asset_id", "canonical_text_hash", "nodes",
    "candidate_records", "known_parent_candidate_ids", "allow_successor",
    "model_profile_revision_id",
})
_RESUME_FIELDS = frozenset({
    "resume_checkpoint_asset_id", "resume_checkpoint_asset_hash",
    "resume_state_asset_id", "resume_state_asset_hash",
})
_RAW_AUTHORITY_FIELDS = frozenset({
    "canonical_text", "path", "file_path", "url", "database", "router",
})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$")
_PAGE_SIZE = 8_388_608
_MAX_PAGES = 4096
_ZERO_HASH = "0" * 64


class DonorAnalysisWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code; self.retryable = retryable
        super().__init__(message)


class TerminalContractError(DonorAnalysisWorkerError):
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
    stage_response: dict[str, object] | None = None
    terminal_dispatched: bool = False
    terminal_outcome: str | None = None


@dataclass
class _ActiveRun:
    worker_run_id: str
    binding_hash: str
    cancelled: threading.Event


class _OperationDispatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock(); self._active: _ActiveRun | None = None

    def begin(self, worker_run_id: str, binding_hash: str) -> _ActiveRun:
        with self._lock:
            if self._active is not None:
                raise DonorAnalysisWorkerError("WORKER_BUSY", "donor-analysis already has an active operation", retryable=True)
            self._active = _ActiveRun(worker_run_id, binding_hash, threading.Event())
            return self._active

    def cancel(self, worker_run_id: str, binding_hash: str) -> bool:
        with self._lock:
            active = self._active
            if active is None or active.worker_run_id != worker_run_id or active.binding_hash != binding_hash:
                return False
            active.cancelled.set(); return True

    def finish(self, active: _ActiveRun) -> None:
        with self._lock:
            if self._active is active: self._active = None


_DISPATCHER = _OperationDispatcher()


def _identity() -> tuple[str, str]:
    try: value = load_runtime_identity()
    except Exception as exc: raise DonorAnalysisWorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc
    return str(value["package_hash"]), str(value["release_id"])


def _require_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or any(value is None for value in (
        _canonical_bytes_sdk, _validate_rpc_result_sdk, _verify_checkpoint_sdk,
        _verify_provenance_receipt_sdk, _verify_result_bundle_sdk,
    )):
        raise DonorAnalysisWorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; donor-analysis is fail-closed") from _SDK_IMPORT_ERROR


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DonorAnalysisWorkerError("INPUT_INVALID", f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise DonorAnalysisWorkerError("INPUT_INVALID", f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DonorAnalysisWorkerError("INPUT_INVALID", f"{label} must be an integer >= {minimum}")
    return value


def _hash_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    _require_sdk(); assert _canonical_bytes_sdk is not None
    try: return bytes(_canonical_bytes_sdk(value))
    except Exception as exc: raise DonorAnalysisWorkerError("CANONICALIZATION_ERROR", str(exc)) from exc


def _strict_json(raw: bytes, *, code: str = "ASSET_READ_ERROR") -> Any:
    def duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=duplicate,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except Exception as exc: raise DonorAnalysisWorkerError(code, "Asset is not strict UTF-8 JSON") from exc


def _host_call(host: HostPort | None, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    if host is None or not hasattr(host, "call"):
        raise DonorAnalysisWorkerError("HOST_REQUIRED", "Core HostPort.call is required")
    request_params = dict(params)
    try: response = host.call(method, request_params)
    except DonorAnalysisWorkerError: raise
    except Exception as exc: raise DonorAnalysisWorkerError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}", retryable=True) from exc
    if not isinstance(response, Mapping):
        raise DonorAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    assert _validate_rpc_result_sdk is not None
    try: _validate_rpc_result_sdk(method, dict(response), request={"method": method, "params": request_params})
    except Exception as exc: raise DonorAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} result failed public RPC schema: {exc}") from exc
    return dict(response)


def _read_asset(host: HostPort | None, asset_id: str, expected_hash: str | None) -> bytes:
    _id(asset_id, "asset_id")
    if expected_hash is not None: _hash(expected_hash, "expected Asset hash")
    offset = 0; chunks: list[bytes] = []
    for _ in range(_MAX_PAGES):
        response = _host_call(host, "host.asset.read/v1", {"asset_id": asset_id, "offset": offset, "length": _PAGE_SIZE})
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str): raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset page lacks base64_chunk")
        try: chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc: raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset page base64 is invalid") from exc
        if len(chunk) > _PAGE_SIZE or response.get("content_hash") != _hash_bytes(chunk):
            raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset page length/hash is invalid")
        chunks.append(chunk); expected_next = offset + len(chunk); next_offset = response.get("next_offset")
        if next_offset is None: break
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset != expected_next or next_offset <= offset:
            raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset next_offset is not contiguous")
        offset = next_offset
    else: raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset exceeded bounded page count")
    data = b"".join(chunks)
    if expected_hash is not None and _hash_bytes(data) != expected_hash:
        raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "Asset hash mismatch")
    return data


def _load_json_asset(host: HostPort | None, asset_id: str, expected_hash: str | None) -> Any:
    return _strict_json(_read_asset(host, asset_id, expected_hash))


def _request_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-request/v1", dict(request))


def _binding_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    value = {key: deepcopy(child) for key, child in request.items() if key not in _RESUME_FIELDS}
    value["operation"] = "run"; return value


def _binding_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-binding/v1", _binding_projection(request))


def _capability_fields(capability: str) -> frozenset[str]:
    group = SPEC_BY_CAPABILITY[capability].request_fields_group
    return {
        "source": _SOURCE_FIELDS,
        "manual": _MANUAL_FIELDS,
        "claim": _CLAIM_FIELDS,
        "rereview": _REREVIEW_FIELDS,
    }[group]


def _validate_context(request: Mapping[str, Any], capability: str) -> dict[str, Any]:
    if not isinstance(request, Mapping): raise DonorAnalysisWorkerError("INPUT_INVALID", "request must be an object")
    value = dict(request)
    if any(field in value for field in _RAW_AUTHORITY_FIELDS):
        raise DonorAnalysisWorkerError("INPUT_INVALID", "raw text/path/url/database/router fields are forbidden")
    operation = value.get("operation")
    spec = SPEC_BY_CAPABILITY[capability]
    if operation not in spec.supports:
        raise DonorAnalysisWorkerError("INPUT_INVALID", f"operation {operation!r} is not declared")
    allowed = set(_COMMON_FIELDS | _capability_fields(capability))
    if operation == "resume": allowed.update(_RESUME_FIELDS)
    if set(value) != allowed:
        raise DonorAnalysisWorkerError("INPUT_INVALID", f"request fields are not closed: missing={sorted(allowed-set(value))}, extra={sorted(set(value)-allowed)}")
    if value["schema"] != spec.input_schema or value["capability_id"] != capability or value["operation_key"] != capability:
        raise DonorAnalysisWorkerError("INPUT_INVALID", "request schema/capability/operation_key mismatch")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id", "document_id", "source_revision_id"):
        _id(value[field], field)
    _integer(value["lease_epoch"], "lease_epoch", 1); _integer(value["total_units"], "total_units", 1)
    _hash(value["run_snapshot_hash"], "run_snapshot_hash")
    if not isinstance(value["created_at"], str) or _TIME_RE.fullmatch(value["created_at"]) is None:
        raise DonorAnalysisWorkerError("INPUT_INVALID", "created_at must use frozen RFC3339 UTC form")
    checkpoints = value["checkpoint_ids"]
    if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) != len(set(checkpoints)):
        raise DonorAnalysisWorkerError("INPUT_INVALID", "checkpoint_ids must be non-empty unique")
    for index, child in enumerate(checkpoints): _id(child, f"checkpoint_ids[{index}]")
    for field in ("model_profile_revision_id",):
        if field in value: _id(value[field], field)
    for field in ("canonical_asset_id", "taxonomy_asset_id", "manual_annotation_asset_id", "parameters_asset_id"):
        if field in value: _id(value[field], field)
    for field in ("canonical_text_hash", "taxonomy_asset_hash", "manual_annotation_asset_hash", "parameters_asset_hash"):
        if field in value: _hash(value[field], field)
    if "nodes" in value and not isinstance(value["nodes"], list): raise DonorAnalysisWorkerError("INPUT_INVALID", "nodes must be an array")
    if capability == CAPABILITY_ATOM_MANUAL:
        _id(value["actor_id"], "actor_id")
        if not isinstance(value["atom_payload"], Mapping): raise DonorAnalysisWorkerError("INPUT_INVALID", "atom_payload must be an object")
    if capability == CAPABILITY_CLAIM_GENERATE and not isinstance(value["accepted_atoms"], list):
        raise DonorAnalysisWorkerError("INPUT_INVALID", "accepted_atoms must be an array")
    if capability == CAPABILITY_CLAIM_GENERATE:
        _id(value["snapshot_parameters_asset_id"], "snapshot_parameters_asset_id")
        rows = value["snapshot_asset_hashes"]
        if not isinstance(rows, list) or not rows:
            raise DonorAnalysisWorkerError("INPUT_INVALID", "snapshot_asset_hashes must be non-empty")
        seen_assets: set[str] = set(); bindings: set[tuple[str, str]] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or set(row) != {"asset_id", "sha256"}:
                raise DonorAnalysisWorkerError("INPUT_INVALID", f"snapshot_asset_hashes[{index}] is not closed")
            asset_id = _id(row["asset_id"], "snapshot asset_id"); content_hash = _hash(row["sha256"], "snapshot sha256")
            if asset_id in seen_assets: raise DonorAnalysisWorkerError("INPUT_INVALID", "snapshot Asset identity is duplicated")
            seen_assets.add(asset_id); bindings.add((asset_id, content_hash))
        if value["snapshot_parameters_asset_id"] != value["parameters_asset_id"] or (value["parameters_asset_id"], value["parameters_asset_hash"]) not in bindings:
            raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "parameters Asset is not exactly bound by RunSnapshot parameters_asset_id/asset_hashes")
    if capability == CAPABILITY_REREVIEW:
        if not isinstance(value["candidate_records"], list) or not isinstance(value["known_parent_candidate_ids"], list):
            raise DonorAnalysisWorkerError("INPUT_INVALID", "rereview records/parents must be arrays")
        if len(value["known_parent_candidate_ids"]) != len(set(value["known_parent_candidate_ids"])):
            raise DonorAnalysisWorkerError("INPUT_INVALID", "known parent ids must be unique")
        for child in value["known_parent_candidate_ids"]: _id(child, "known_parent_candidate_id")
        if not isinstance(value["allow_successor"], bool): raise DonorAnalysisWorkerError("INPUT_INVALID", "allow_successor must be boolean")
    if operation == "resume":
        for field in ("resume_checkpoint_asset_id", "resume_state_asset_id"): _id(value[field], field)
        for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"): _hash(value[field], field)
    return value


def _context(request: Mapping[str, Any], capability: str) -> tuple[dict[str, Any], _RunContext]:
    value = _validate_context(request, capability)
    return value, _RunContext(_request_hash(value, capability), _binding_hash(value, capability), tuple(value["checkpoint_ids"]))


def _load_source(value: Mapping[str, Any], host: HostPort | None) -> tuple[str, list[dict[str, Any]]]:
    raw = _read_asset(host, _id(value["canonical_asset_id"], "canonical_asset_id"), _hash(value["canonical_text_hash"], "canonical_text_hash"))
    try: text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc: raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "canonical Asset is not strict UTF-8") from exc
    if sha256_text(text) != value["canonical_text_hash"]: raise DonorAnalysisWorkerError("ASSET_READ_ERROR", "canonical Revision hash mismatch")
    try: nodes = validate_nodes(value["nodes"], len(text))
    except DonorContractError as exc: raise DonorAnalysisWorkerError("EVIDENCE_INVALID", str(exc)) from exc
    return text, nodes


def _upload(host: HostPort | None, context: _RunContext, data: bytes, mime: str, suffix: str) -> str:
    expected_hash = _hash_bytes(data); upload_id = context.request_hash + "-upload-" + suffix
    chunks = [b""] if not data else [data[offset:offset+_PAGE_SIZE] for offset in range(0, len(data), _PAGE_SIZE)]
    accepted = 0; asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks)-1
        response = _host_call(host, "host.asset.create/v1", {
            "operation_key": context.request_hash, "upload_id": upload_id,
            "offset": accepted, "mime": mime, "total_size": len(data),
            "expected_hash": expected_hash, "chunk_hash": _hash_bytes(chunk),
            "base64_chunk": base64.b64encode(chunk).decode("ascii"), "final": final,
        })
        if response.get("upload_id") != upload_id or response.get("accepted_bytes") != accepted+len(chunk) or response.get("completed") is not final:
            raise DonorAnalysisWorkerError("ASSET_CREATE_ERROR", "Asset upload acknowledgement is not contiguous")
        raw_id = response.get("asset_id")
        if final: asset_id = _id(raw_id, "Host final asset_id")
        elif raw_id is not None: raise DonorAnalysisWorkerError("ASSET_CREATE_ERROR", "non-final upload returned Asset ID")
        accepted += len(chunk)
    status = _host_call(host, "host.asset.upload.status/v1", {"upload_id": upload_id, "expected_hash": expected_hash})
    if status.get("accepted_bytes") != len(data) or status.get("completed") is not True or status.get("asset_id") != asset_id:
        raise DonorAnalysisWorkerError("ASSET_UPLOAD_ERROR", "upload status differs from completed Asset")
    assert asset_id is not None; return asset_id


def _record_event(context: _RunContext, response: Mapping[str, object], method: str) -> None:
    if response.get("accepted") is not True: raise DonorAnalysisWorkerError("HOST_REJECTED", f"Core rejected {method}")
    sequence = response.get("job_event_seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= context.last_job_event_seq:
        raise DonorAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} job_event_seq is not strictly monotonic")
    context.last_job_event_seq = sequence


def _event(host: HostPort | None, context: _RunContext, event_type: str, payload_asset_id: str | None, local_seq: int) -> None:
    if local_seq <= context.last_local_seq: raise DonorAnalysisWorkerError("HOST_CONTRACT_ERROR", "local event sequence is not monotonic")
    response = _host_call(host, "host.job.event/v1", {"operation_key": context.request_hash, "event_type": event_type, "payload_asset_id": payload_asset_id, "local_seq": local_seq})
    _record_event(context, response, "host.job.event/v1"); context.last_local_seq = local_seq


def _producer(request: Mapping[str, Any], capability: str, release_id: str) -> dict[str, Any]:
    return {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": capability,
            "job_id": request["job_id"], "step_id": request["step_id"],
            "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"]}


def _bundle(request: Mapping[str, Any], capability: str, items: list[dict[str, Any]], release_id: str, *, partial: bool = False) -> dict[str, Any]:
    spec = SPEC_BY_CAPABILITY[capability]
    contract_id, bundle_type = (("diagnostic-bundle/v1", "diagnostic") if partial else
                                (spec.result_contract, spec.bundle_type))
    request_hash = _request_hash(request, capability)
    bundle = {
        "schema": "result-bundle/v1", "contract_id": contract_id,
        "bundle_id": _derived_id("bundle", request_hash, contract_id), "bundle_type": bundle_type,
        "producer": _producer(request, capability, release_id),
        "input_snapshot_hash": request["run_snapshot_hash"], "items": items,
        "warnings": [], "partial": partial,
        "provenance_receipt_id": request["provenance_receipt_id"],
        "skill_chain_result_refs": [],
    }
    _require_sdk(); assert _verify_result_bundle_sdk is not None
    try:
        _verify_result_bundle_sdk(bundle,
            snapshot_workspace_id=request.get("workspace_id") if contract_id == "candidate-batch/v1" else None,
            snapshot_hash_value=request["run_snapshot_hash"],
            known_parent_ids=set(request.get("known_parent_candidate_ids", [])),
            attempt_state="failed" if partial else None)
    except Exception as exc: raise DonorAnalysisWorkerError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return bundle


def _derived_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256((prefix + "\n" + "\n".join(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:48]}"


def _source_ref(request: Mapping[str, Any], source_id: str | None = None, revision_or_hash: str | None = None) -> dict[str, Any]:
    return {"workspace_id": request["workspace_id"], "source_type": "canonical_revision",
            "source_id": source_id or request["document_id"],
            "revision_or_hash": revision_or_hash or request["source_revision_id"]}


def _model_receipt_ref(request: Mapping[str, Any], receipt_id: str) -> dict[str, Any]:
    return {"workspace_id": None, "source_type": "model_receipt",
            "source_id": receipt_id,
            "revision_or_hash": request["model_profile_revision_id"]}


def _candidate_item(request: Mapping[str, Any], context: _RunContext, payload: Mapping[str, Any], payload_asset_id: str, payload_schema: str, ordinal: int, *, parent_ids: list[str] | None = None) -> dict[str, Any]:
    payload_hash = _hash_bytes(_json_bytes(payload)); item_id = _derived_id("candidate", context.request_hash, str(ordinal), payload_hash)
    entity_id = ("book-atom:" if payload_schema == "book-atom/v1" else "book-claim:") + item_id.split(":",1)[1]
    base_hash = request.get("canonical_text_hash", request.get("parameters_asset_hash"))
    assert isinstance(base_hash, str)
    return {
        "schema": "candidate-item/v1", "item_id": item_id, "item_kind": "relation_set",
        "target": {"workspace_id": request["workspace_id"], "entity_kind": "relation_set", "entity_id": entity_id},
        "mutation": {"mode": "relation_patch", "payload_schema": payload_schema, "payload_hash": payload_hash},
        "payload_asset_id": payload_asset_id,
        "base": {"revision_id": request["source_revision_id"], "content_hash": base_hash},
        "write_set": [{"workspace_id": request["workspace_id"], "entity_kind": "relation_set", "entity_id": entity_id,
                       "revision_id": request["source_revision_id"], "content_hash": base_hash}],
        "parent_candidate_ids": list(parent_ids or []),
        "source_refs": [_source_ref(request)], "status": "complete",
    }


def _diagnostic_item(request: Mapping[str, Any], context: _RunContext, ordinal: int, *, severity: str, code: str, message: str, details: Mapping[str, Any], host: HostPort | None, status: str = "complete", include_source_ref: bool = True) -> dict[str, Any]:
    data = _json_bytes(details); asset_id = _upload(host, context, data, "application/json", f"diagnostic-{ordinal}")
    return {"schema": "diagnostic-item/v1", "item_id": _derived_id("diagnostic", context.request_hash, str(ordinal), code),
            "severity": severity, "code": code, "message": message,
            "details_asset_id": asset_id, "details_hash": _hash_bytes(data),
            "source_refs": [_source_ref(request)] if include_source_ref else [], "status": status}


def _stage(host: HostPort | None, request: Mapping[str, Any], context: _RunContext,
           bundle_asset_id: str, bundle_content_hash: str,
           items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not items: raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "Candidate stage requires emitted items")
    _hash(bundle_content_hash, "result Bundle content hash")
    # The binding deliberately excludes the run/resume transport envelope.  A
    # process that stops after Core accepted staging must therefore replay the
    # exact same logical operation, rather than create a second Candidate set.
    stage_key = _derived_id("candidate-stage", context.binding_hash, bundle_content_hash)
    response = _host_call(host, "host.candidate.stage/v1", {"operation_key": stage_key, "result_bundle_asset_id": bundle_asset_id, "input_snapshot_hash": request["run_snapshot_hash"]})
    if response.get("accepted") is not True: raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "Core rejected Candidate stage")
    rows = response.get("staged_items"); expected_ids = [item["item_id"] for item in items]
    if not isinstance(rows, list) or len(rows) != len(expected_ids): raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage rows cardinality mismatch")
    actual: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != {"item_id", "candidate_id", "stage_status", "publication_eligibility"}:
            raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage row is not closed")
        actual.append(_id(row["item_id"], "staged item_id")); _id(row["candidate_id"], "candidate_id")
        if row["stage_status"] not in {"created", "existing"}: raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "invalid stage status")
    if actual != expected_ids or len(actual) != len(set(actual)): raise DonorAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage rows are not ordered one-to-one")
    sequence = response.get("job_event_seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise DonorAnalysisWorkerError("HOST_CONTRACT_ERROR", "host.candidate.stage/v1 job_event_seq is invalid")
    # An idempotent replay returns the original Core event acknowledgement.  It
    # may therefore be below this attempt's current high-water mark, but its
    # operation key, bundle binding, and ordered staged rows remain exact.
    if sequence > context.last_job_event_seq: context.last_job_event_seq = sequence
    context.stage_response = deepcopy(response)
    return stage_key, actual


def _receipt(request: Mapping[str, Any], capability: str, package_hash: str, release_id: str,
             bundle: Mapping[str, Any] | None, staged_ids: list[str], model_receipt_ids: list[str],
             skill_chain_result_refs: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    if len(staged_ids) != len(set(staged_ids)) or len(model_receipt_ids) != len(set(model_receipt_ids)):
        raise DonorAnalysisWorkerError("RESULT_CONTRACT_ERROR", "receipt item identities must be unique")
    receipt = {
        "schema": "provenance-receipt/v1", "receipt_id": request["provenance_receipt_id"],
        "plugin_id": PLUGIN_ID, "release_id": release_id, "package_hash": package_hash,
        "capability_id": capability, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"],
        "bundle_id": None if bundle is None else bundle["bundle_id"],
        "bundle_hash": None if bundle is None else hash_json("result-bundle/v1", bundle),
        "parent_receipt_ids": [], "model_receipt_ids": model_receipt_ids,
        "skill_chain_result_refs": deepcopy(list(skill_chain_result_refs or [])), "staged_items": staged_ids,
        "created_at": request["created_at"],
    }
    receipt["receipt_hash"] = hash_json("provenance-receipt/v1", receipt)
    _require_sdk(); assert _verify_provenance_receipt_sdk is not None
    try: _verify_provenance_receipt_sdk(receipt)
    except Exception as exc: raise DonorAnalysisWorkerError("RECEIPT_CONTRACT_ERROR", str(exc)) from exc
    return receipt


def _complete(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, outcome: str,
              bundle_asset_id: str | None, stage_key: str | None, detail_asset_id: str | None, local_seq: int) -> None:
    if context.terminal_dispatched:
        raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED", "terminal completion may be dispatched at most once")
    # Set ownership before crossing the RPC boundary.  A transport exception or
    # a malformed acknowledgement may happen after Core durably accepted the
    # terminal transition; neither case permits a compensating second terminal.
    context.terminal_dispatched = True
    context.terminal_outcome = outcome
    response = _host_call(host, "host.job.complete/v1", {"operation_key": context.request_hash, "worker_run_id": request["worker_run_id"],
        "outcome": outcome, "result_bundle_asset_id": bundle_asset_id, "candidate_stage_operation_key": stage_key,
        "terminal_detail_asset_id": detail_asset_id, "local_seq": local_seq})
    _record_event(context, response, "host.job.complete/v1")
    expected_attempt = {"succeeded":"succeeded", "failed":"failed", "cancelled":"cancelled", "partial":"failed"}[outcome]
    if response.get("attempt_state") != expected_attempt or response.get("provenance_receipt_id") != request["provenance_receipt_id"]:
        raise TerminalContractError("TERMINAL_CONTRACT_ERROR", "Host terminal acknowledgement identity/state mismatch")
    context.last_local_seq = local_seq


def _checkpoint(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, state_asset_id: str, state_hash: str) -> dict[str, Any]:
    if context.last_checkpoint_seq >= len(context.checkpoint_ids): raise DonorAnalysisWorkerError("CHECKPOINT_ERROR", "checkpoint budget exhausted")
    checkpoint = {"schema": "checkpoint/v1", "checkpoint_id": context.checkpoint_ids[context.last_checkpoint_seq],
        "checkpoint_seq": context.last_checkpoint_seq+1, "job_id": request["job_id"], "step_id": request["step_id"],
        "source_attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"], "replay_policy": "checkpoint_resume",
        "completed_units": request["total_units"], "total_units": request["total_units"],
        "unit_set_hash": state_hash, "state_asset_id": state_asset_id, "created_at": request["created_at"]}
    checkpoint["checkpoint_hash"] = hash_json("checkpoint/v1", checkpoint)
    _require_sdk(); assert _verify_checkpoint_sdk is not None
    try: _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"], previous_seq=None if context.last_checkpoint_seq == 0 else context.last_checkpoint_seq)
    except Exception as exc: raise DonorAnalysisWorkerError("CHECKPOINT_CONTRACT_ERROR", str(exc)) from exc
    data = _json_bytes(checkpoint); checkpoint_asset_id = _upload(host, context, data, "application/json", "checkpoint")
    response = _host_call(host, "host.checkpoint.commit/v1", {"operation_key": context.request_hash+"-checkpoint", "checkpoint_asset_id": checkpoint_asset_id})
    _record_event(context, response, "host.checkpoint.commit/v1")
    if response.get("checkpoint_id") != checkpoint["checkpoint_id"] or response.get("completed_units") != request["total_units"] or response.get("total_units") != request["total_units"]:
        raise DonorAnalysisWorkerError("CHECKPOINT_ERROR", "Host checkpoint acknowledgement mismatch")
    context.last_checkpoint_seq += 1
    return {"checkpoint": checkpoint, "checkpoint_asset_id": checkpoint_asset_id,
            "checkpoint_asset_hash": _hash_bytes(data), "state_asset_id": state_asset_id,
            "state_asset_hash": state_hash, "state_hash": state_hash}


def _state(request: Mapping[str, Any], context: _RunContext, bundle: Mapping[str, Any],
           bundle_asset_id: str, bundle_hash: str, status: str,
           model_receipt_ids: list[str], package_hash: str, release_id: str,
           bundle_provenance_hash: str) -> dict[str, Any]:
    return {"schema": "donor-analysis-resume-state/v1",
            "binding_hash": context.binding_hash,
            "plugin_id": PLUGIN_ID, "package_hash": package_hash, "release_id": release_id,
            "capability_id": request["capability_id"],
            "job_id": request["job_id"], "step_id": request["step_id"],
            "attempt_id": request["attempt_id"], "worker_run_id": request["worker_run_id"],
            "lease_epoch": request["lease_epoch"], "created_at": request["created_at"],
            "provenance_receipt_id": request["provenance_receipt_id"],
            "workspace_id": request["workspace_id"], "document_id": request["document_id"],
            "source_revision_id": request["source_revision_id"],
            "run_snapshot_hash": request["run_snapshot_hash"],
            "result_bundle_asset_id": bundle_asset_id,
            "result_bundle_hash": bundle_hash,
            "bundle_provenance_hash": bundle_provenance_hash,
            "candidate_stage_operation_key": (
                _derived_id("candidate-stage", context.binding_hash, bundle_hash)
                if bundle.get("contract_id") == "candidate-batch/v1" else None
            ),
            "checkpoint_id": context.checkpoint_ids[context.last_checkpoint_seq],
            "checkpoint_seq": context.last_checkpoint_seq + 1,
            "model_receipt_ids": list(model_receipt_ids),
            "skill_chain_result_refs": deepcopy(list(bundle.get("skill_chain_result_refs", []))),
            "status": status}


def _invoke_model(host: HostPort | None, request: Mapping[str, Any], context: _RunContext,
                  capability: str, model_request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    data = _json_bytes(model_request)
    request_asset_id = _upload(host, context, data, "application/json", "model-request")
    invocation_id = _derived_id("invocation", context.request_hash)
    response = _host_call(host, "host.model.invoke/v1", {
        "operation_key": context.request_hash + "-model", "invocation_id": invocation_id,
        "invocation_key": _derived_id("model-key", context.binding_hash, capability),
        "model_profile_revision_id": request["model_profile_revision_id"],
        "request_asset_id": request_asset_id, "replay_policy": "manual_if_unknown",
    })
    if response.get("state") != "received" or response.get("response_asset_id") is None or response.get("uncertainty") is not None:
        raise DonorAnalysisWorkerError("MODEL_INVOKE_ERROR", "model invocation did not return a certain received response", retryable=response.get("state") == "uncertain")
    receipt_id = _id(response.get("receipt_id"), "model receipt_id")
    response_asset_id = _id(response.get("response_asset_id"), "model response_asset_id")
    output = _load_json_asset(host, response_asset_id, None)
    if not isinstance(output, dict): raise DonorAnalysisWorkerError("MODEL_OUTPUT_INVALID", "model response Asset must be an object")
    return output, receipt_id


_TAXONOMY_FIELDS = {"schema", "format_id", "taxonomy_id", "version", "atom_kinds",
    "description_types", "narration_techniques", "ratio_metrics", "rhythm_emotion_metrics",
    "language_metrics", "structure_metrics", "review_rubric", "interpreter_mappings"}
_TAXONOMY_KEY_SECTIONS = ("atom_kinds", "description_types", "narration_techniques",
    "ratio_metrics", "rhythm_emotion_metrics", "language_metrics", "structure_metrics")


def _validate_taxonomy(host: HostPort | None, request: Mapping[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    value = _load_json_asset(host, request["taxonomy_asset_id"], request["taxonomy_asset_hash"])
    if not isinstance(value, dict) or value.get("schema") != "book-analysis-taxonomy/v1" or value.get("format_id") != "book-analysis-taxonomy/v1":
        raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "taxonomy Asset is not book-analysis-taxonomy/v1")
    if set(value) != _TAXONOMY_FIELDS:
        raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "taxonomy Asset is not closed")
    _id(value.get("taxonomy_id"), "taxonomy.taxonomy_id")
    if value.get("version") != "1.0.0": raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "taxonomy version is not frozen v1")
    allowed: set[str] = set()
    for section in _TAXONOMY_KEY_SECTIONS:
        rows = value.get(section)
        if not isinstance(rows, list) or not rows: raise DonorAnalysisWorkerError("TAXONOMY_INVALID", f"taxonomy {section} is empty")
        for row in rows:
            expected_row_fields = ({"key", "label_zh"} if section in {"atom_kinds", "description_types", "narration_techniques"}
                                   else {"key", "label_zh", "value_type", "allowed_values"})
            if not isinstance(row, Mapping) or set(row) != expected_row_fields or not isinstance(row.get("key"), str) or not row["key"]:
                raise DonorAnalysisWorkerError("TAXONOMY_INVALID", f"taxonomy {section} row lacks key")
            allowed.add(row["key"])
    rubric = value.get("review_rubric")
    if not isinstance(rubric, list) or not rubric:
        raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "review rubric must be non-empty")
    for row in rubric:
        if not isinstance(row, Mapping) or set(row) != {"criterion_id", "label_zh", "applies_to", "requirement", "failure_code"}:
            raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "review rubric row is not closed")
    mappings = value.get("interpreter_mappings")
    expected = {(capability, direction) for capability in CAPABILITIES for direction in ("format_to_capability", "capability_to_format")}
    actual: set[tuple[str, str]] = set()
    if not isinstance(mappings, list): raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "interpreter mappings must be an array")
    for row in mappings:
        if not isinstance(row, Mapping) or set(row) != {"format_id", "plugin_id", "capability_id", "direction"}:
            raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "interpreter mapping is not closed")
        if row["format_id"] != "book-analysis-taxonomy/v1" or row["plugin_id"] != PLUGIN_ID:
            raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "interpreter mapping owner changed")
        actual.add((str(row["capability_id"]), str(row["direction"])))
    if actual != expected or len(mappings) != len(expected):
        raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "taxonomy must cover exactly four capabilities in both directions")
    if frozenset(allowed) != ATOM_KINDS:
        raise DonorAnalysisWorkerError("TAXONOMY_INVALID", "Code/Data frozen atom kind union drifted")
    return value, frozenset(allowed)


def _validate_model_envelope(output: Mapping[str, Any], schema: str) -> list[dict[str, Any]]:
    if set(output) != {"schema", "items"} or output.get("schema") != schema or not isinstance(output.get("items"), list) or not output["items"]:
        raise DonorAnalysisWorkerError("MODEL_OUTPUT_INVALID", "model output envelope is not exact/non-empty")
    if any(not isinstance(item, Mapping) for item in output["items"]):
        raise DonorAnalysisWorkerError("MODEL_OUTPUT_INVALID", "model output item must be an object")
    return [dict(item) for item in output["items"]]


def _trusted_atom_provenance(
    request: Mapping[str, Any], *, mode: str, analysis_method: Mapping[str, Any],
    package_hash: str, release_id: str, model_receipt_id: str | None = None,
) -> dict[str, Any]:
    try:
        return build_atom_provenance(
            mode=mode, analysis_method=analysis_method, plugin_id=PLUGIN_ID,
            package_hash=package_hash, release_id=release_id,
            capability_id=request["capability_id"], job_id=request["job_id"],
            step_id=request["step_id"], attempt_id=request["attempt_id"],
            worker_run_id=request["worker_run_id"], lease_epoch=request["lease_epoch"],
            provenance_receipt_id=request["provenance_receipt_id"],
            run_snapshot_hash=request["run_snapshot_hash"], workspace_id=request["workspace_id"],
            document_id=request["document_id"], source_revision_id=request["source_revision_id"],
            canonical_text_hash=request["canonical_text_hash"], created_at=request["created_at"],
            model_profile_revision_id=request.get("model_profile_revision_id") if mode == "model" else None,
            model_receipt_id=model_receipt_id if mode == "model" else None,
            actor_id=request.get("actor_id") if mode == "manual" else None,
            annotation_asset_id=request.get("manual_annotation_asset_id") if mode == "manual" else None,
            annotation_asset_hash=request.get("manual_annotation_asset_hash") if mode == "manual" else None,
        )
    except DonorContractError as exc:
        raise DonorAnalysisWorkerError("ATOM_INVALID", str(exc)) from exc


def _validate_accepted_payload(payload: Any, snapshot: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != set(ATOM_FIELDS) or payload.get("schema") != "book-atom/v1" or payload.get("authority") != "candidate_only":
        raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "accepted Atom payload is not candidate-only book-atom/v1")
    spans = payload.get("evidence_spans")
    if spans != snapshot["evidence_spans"]:
        raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "accepted Atom payload evidence differs from acceptance snapshot")
    if not isinstance(spans, list): raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "accepted Atom evidence is not an array")
    for span in spans:
        if not isinstance(span, Mapping) or set(span) != set(EVIDENCE_FIELDS) or span.get("workspace_id") != request["workspace_id"] or span.get("document_id") != request["document_id"] or span.get("revision_id") != snapshot["source_revision_id"]:
            raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "accepted Atom evidence source Revision changed")
        quote = span.get("quote")
        start, end = span.get("start_codepoint"), span.get("end_codepoint")
        if (not isinstance(quote, str) or not quote or span.get("quote_hash") != sha256_text(quote)
            or not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool)
            or start < 0 or end <= start or not isinstance(span.get("canonical_text_hash"), str)
            or HASH_RE.fullmatch(span["canonical_text_hash"]) is None):
            raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", "accepted Atom evidence quote hash changed")


def _load_claim_authority(
    request: Mapping[str, Any], host: HostPort | None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Load the sole authoritative Claim input and revalidate every accepted Atom."""
    canonical_text, nodes = _load_source(request, host)
    raw_input = _load_json_asset(
        host, request["parameters_asset_id"], request["parameters_asset_hash"]
    )
    try:
        claim_input = validate_claim_input(
            raw_input, canonical_text=canonical_text, nodes=nodes,
            workspace_id=request["workspace_id"], document_id=request["document_id"],
            source_revision_id=request["source_revision_id"],
            canonical_text_hash=request["canonical_text_hash"],
        )
        accepted = bind_current_accepted_atoms(claim_input, request["accepted_atoms"])
    except DonorContractError as exc:
        raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", str(exc)) from exc

    for snapshot in accepted:
        payload = _strict_json(_read_asset(
            host, snapshot["payload_asset_id"], snapshot["payload_hash"]
        ))
        _validate_accepted_payload(payload, snapshot, request)
        provenance = payload.get("provenance") if isinstance(payload, Mapping) else None
        mode = provenance.get("mode") if isinstance(provenance, Mapping) else None
        if mode not in {"model", "manual"}:
            raise DonorAnalysisWorkerError(
                "CLAIM_AUTHORITY_INVALID", "accepted Atom lacks closed provenance"
            )
        try:
            validate_atom_payload(
                payload, canonical_text=canonical_text, nodes=nodes,
                workspace_id=request["workspace_id"], document_id=request["document_id"],
                revision_id=request["source_revision_id"],
                canonical_text_hash=request["canonical_text_hash"],
                expected_mode=mode, allowed_atom_kinds=ATOM_KINDS,
            )
        except DonorContractError as exc:
            raise DonorAnalysisWorkerError("CLAIM_AUTHORITY_INVALID", str(exc)) from exc
    return canonical_text, nodes, claim_input


_REREVIEW_DETAIL_FIELDS = frozenset({
    "schema", "candidate_id", "recommendation", "successor", "lineage",
    "mutation_staged",
})


def _validate_rereview_detail_authority(
    records: list[dict[str, Any]], details: list[Mapping[str, Any]], *, code: str,
) -> None:
    """Bind the complete ordered diagnostic projection to request Candidate records."""
    expected_ids = [record["candidate_id"] for record in records]
    actual_ids = [item.get("candidate_id") for item in details]
    if len(details) != len(records) or actual_ids != expected_ids:
        raise DonorAnalysisWorkerError(
            code,
            "rereview Bundle diagnostics must map ordered one-to-one to every input Candidate",
        )
    for index, (record, item) in enumerate(zip(records, details, strict=True)):
        predecessor = record["parent_candidate_id"]
        successor = record["successor_candidate_id"]
        expected_successor = None if successor is None else {
            "mode": "existing_idempotent",
            "predecessor_candidate_id": predecessor,
            "successor_candidate_id": successor,
        }
        if (set(item) != _REREVIEW_DETAIL_FIELDS
                or item.get("schema") != "book-rereview-diagnostic/v1"
                or item.get("mutation_staged") is not False
                or item.get("lineage") != {
                    "predecessor_candidate_id": predecessor,
                    "successor_candidate_id": successor,
                }
                or item.get("successor") != expected_successor):
            raise DonorAnalysisWorkerError(
                code, f"rereview diagnostic lineage changed at input position {index}"
            )


def _bundle_payload_provenance(
    host: HostPort | None, request: Mapping[str, Any], capability: str,
    bundle: Mapping[str, Any], package_hash: str, release_id: str,
) -> tuple[str, list[str] | None]:
    """Re-read Bundle payloads and derive provenance without trusting resume state."""
    projection: dict[str, Any] = {
        "schema": "donor-analysis-bundle-provenance/v1",
        "producer": deepcopy(bundle.get("producer")),
        "input_snapshot_hash": bundle.get("input_snapshot_hash"),
        "provenance_receipt_id": bundle.get("provenance_receipt_id"),
        "items": [],
    }
    atom_receipts: list[str] = []
    bundle_model_receipts: list[str] = []
    saw_atom = False
    canonical_text: str | None = None
    nodes: list[dict[str, Any]] | None = None
    claim_input: dict[str, Any] | None = None
    rereview_records: list[dict[str, Any]] = []
    rereview_details: list[Mapping[str, Any]] = []
    if capability == CAPABILITY_CLAIM_GENERATE:
        try:
            canonical_text, nodes, claim_input = _load_claim_authority(request, host)
        except DonorAnalysisWorkerError as exc:
            raise DonorAnalysisWorkerError(
                "RESUME_INVALID", f"Claim resume authority validation failed: {exc}"
            ) from exc
    elif capability == CAPABILITY_REREVIEW:
        try:
            rereview_records = validate_rereview_records(
                request["candidate_records"],
                source_revision_id=request["source_revision_id"],
                known_parent_ids=set(request["known_parent_candidate_ids"]),
            )
        except DonorContractError as exc:
            raise DonorAnalysisWorkerError(
                "RESUME_INVALID", f"rereview request authority changed: {exc}"
            ) from exc
    for index, raw_item in enumerate(bundle.get("items", [])):
        if not isinstance(raw_item, Mapping):
            raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle item is not an object")
        item = dict(raw_item)
        item_projection: dict[str, Any] = {
            "item_id": item.get("item_id"),
            "source_refs": deepcopy(item.get("source_refs")),
            "status": item.get("status"),
        }
        source_refs = item.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs or source_refs[0] != _source_ref(request):
            raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle canonical source Revision changed")
        for ref in source_refs[1:]:
            if (not isinstance(ref, Mapping) or set(ref) != {
                    "workspace_id", "source_type", "source_id", "revision_or_hash"
                } or ref.get("workspace_id") is not None
                or ref.get("source_type") != "model_receipt"
                or ref.get("revision_or_hash") != request.get("model_profile_revision_id")):
                raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle model provenance ref changed")
            receipt_id = _id(ref.get("source_id"), "Bundle model receipt_id")
            if receipt_id not in bundle_model_receipts:
                bundle_model_receipts.append(receipt_id)
        if item.get("schema") == "candidate-item/v1":
            mutation = item.get("mutation")
            if not isinstance(mutation, Mapping):
                raise DonorAnalysisWorkerError("RESUME_INVALID", "Candidate mutation is unavailable")
            payload_hash = _hash(mutation.get("payload_hash"), "Candidate payload_hash")
            payload_asset_id = _id(item.get("payload_asset_id"), "Candidate payload_asset_id")
            payload = _strict_json(_read_asset(host, payload_asset_id, payload_hash))
            if not isinstance(payload, Mapping):
                raise DonorAnalysisWorkerError("RESUME_INVALID", "Candidate payload is not an object")
            payload_schema = mutation.get("payload_schema")
            if payload_schema != payload.get("schema"):
                raise DonorAnalysisWorkerError("RESUME_INVALID", "Candidate payload schema changed")
            if canonical_text is None or nodes is None:
                canonical_text, nodes = _load_source(request, host)
            item_projection.update({
                "payload_asset_id": payload_asset_id,
                "payload_hash": payload_hash,
                "payload_schema": payload_schema,
                "base": deepcopy(item.get("base")),
                "write_set": deepcopy(item.get("write_set")),
                "parent_candidate_ids": deepcopy(item.get("parent_candidate_ids")),
            })
            if payload_schema == "book-atom/v1":
                saw_atom = True
                provenance = payload.get("provenance")
                if not isinstance(provenance, Mapping):
                    raise DonorAnalysisWorkerError("RESUME_INVALID", "Atom provenance is unavailable")
                mode = provenance.get("mode")
                model = provenance.get("model")
                model_receipt_id = None
                if mode == "model":
                    if not isinstance(model, Mapping):
                        raise DonorAnalysisWorkerError("RESUME_INVALID", "model Atom receipt is unavailable")
                    if model.get("profile_revision_id") != request.get("model_profile_revision_id"):
                        raise DonorAnalysisWorkerError("RESUME_INVALID", "Atom model profile changed")
                    model_receipt_id = _id(model.get("receipt_id"), "Atom model receipt_id")
                    if model_receipt_id not in atom_receipts:
                        atom_receipts.append(model_receipt_id)
                expected = _trusted_atom_provenance(
                    request, mode=str(mode),
                    analysis_method=payload.get("analysis_method", {}),
                    package_hash=package_hash, release_id=release_id,
                    model_receipt_id=model_receipt_id,
                )
                if provenance != expected:
                    raise DonorAnalysisWorkerError("RESUME_INVALID", "Atom provenance binding changed")
                try:
                    validate_atom_payload(
                        payload, canonical_text=canonical_text, nodes=nodes,
                        workspace_id=request["workspace_id"], document_id=request["document_id"],
                        revision_id=request["source_revision_id"],
                        canonical_text_hash=request["canonical_text_hash"],
                        expected_mode=str(mode), allowed_atom_kinds=ATOM_KINDS,
                        expected_provenance=expected,
                    )
                except DonorContractError as exc:
                    raise DonorAnalysisWorkerError("RESUME_INVALID", str(exc)) from exc
                item_projection["payload_provenance"] = deepcopy(expected)
            elif payload_schema == "book-claim/v1":
                if capability != CAPABILITY_CLAIM_GENERATE or claim_input is None:
                    raise DonorAnalysisWorkerError(
                        "RESUME_INVALID", "Claim payload is not owned by Claim generation"
                    )
                try:
                    claim = validate_claim_payload(payload, claim_input=claim_input)
                except DonorContractError as exc:
                    raise DonorAnalysisWorkerError(
                        "RESUME_INVALID", f"resumed Claim differs from authoritative input: {exc}"
                    ) from exc
                item_projection["payload_provenance"] = {
                    "ordered_atom_ids": deepcopy(claim["ordered_atom_ids"]),
                    "evidence_spans": deepcopy(claim["evidence_spans"]),
                    "analysis_method": deepcopy(claim["analysis_method"]),
                }
            else:
                raise DonorAnalysisWorkerError("RESUME_INVALID", "Candidate payload schema is unknown")
        elif item.get("schema") == "diagnostic-item/v1":
            details_hash = _hash(item.get("details_hash"), "diagnostic details_hash")
            details_asset_id = _id(item.get("details_asset_id"), "diagnostic details_asset_id")
            details = _strict_json(_read_asset(host, details_asset_id, details_hash))
            if not isinstance(details, Mapping):
                raise DonorAnalysisWorkerError("RESUME_INVALID", "diagnostic details are not an object")
            if capability == CAPABILITY_REREVIEW:
                rereview_details.append(details)
            item_projection.update({
                "details_asset_id": details_asset_id,
                "details_hash": details_hash,
                "details": deepcopy(details),
            })
        else:
            raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle item schema is unknown")
        projection["items"].append(item_projection)
    if capability == CAPABILITY_REREVIEW:
        _validate_rereview_detail_authority(
            rereview_records, rereview_details, code="RESUME_INVALID"
        )
    if saw_atom and bundle_model_receipts:
        raise DonorAnalysisWorkerError("RESUME_INVALID", "Atom Bundle duplicated model provenance")
    derived_receipts = atom_receipts if saw_atom else (bundle_model_receipts or None)
    return hash_json("donor-analysis-bundle-provenance/v1", projection), derived_receipts


_RESUME_STATE_FIELDS = frozenset({
    "schema", "binding_hash", "plugin_id", "package_hash", "release_id",
    "capability_id", "job_id", "step_id", "attempt_id", "worker_run_id",
    "lease_epoch", "created_at", "provenance_receipt_id", "workspace_id",
    "document_id", "source_revision_id", "run_snapshot_hash",
    "result_bundle_asset_id", "result_bundle_hash", "bundle_provenance_hash",
    "candidate_stage_operation_key", "checkpoint_id", "checkpoint_seq",
    "model_receipt_ids", "skill_chain_result_refs", "status",
})


def _validate_resume_binding(
    request: Mapping[str, Any], capability: str, context: _RunContext,
    checkpoint: Mapping[str, Any], state: Mapping[str, Any], bundle: Mapping[str, Any],
    package_hash: str, release_id: str, bundle_provenance_hash: str,
    derived_model_receipt_ids: list[str] | None,
    receipt: Mapping[str, Any] | None = None,
) -> None:
    """Central exact fence for every durable object participating in resume."""
    if set(state) != _RESUME_STATE_FIELDS or state.get("schema") != "donor-analysis-resume-state/v1":
        raise DonorAnalysisWorkerError("RESUME_INVALID", "resume state is not closed")
    state_expected = {
        "binding_hash": context.binding_hash,
        "plugin_id": PLUGIN_ID,
        "package_hash": package_hash,
        "release_id": release_id,
        "capability_id": capability,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "attempt_id": request["attempt_id"],
        "worker_run_id": request["worker_run_id"],
        "lease_epoch": request["lease_epoch"],
        "created_at": request["created_at"],
        "provenance_receipt_id": request["provenance_receipt_id"],
        "workspace_id": request["workspace_id"],
        "document_id": request["document_id"],
        "source_revision_id": request["source_revision_id"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "status": "ready",
        "bundle_provenance_hash": bundle_provenance_hash,
    }
    if any(state.get(field) != expected for field, expected in state_expected.items()):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "resume state binding changed")

    checkpoint_seq = checkpoint.get("checkpoint_seq")
    checkpoint_expected = {
        "job_id": request["job_id"], "step_id": request["step_id"],
        "source_attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"],
        "created_at": request["created_at"], "run_snapshot_hash": request["run_snapshot_hash"],
        "replay_policy": "checkpoint_resume", "completed_units": request["total_units"],
        "total_units": request["total_units"],
        "state_asset_id": request["resume_state_asset_id"],
        "unit_set_hash": request["resume_state_asset_hash"],
    }
    if any(checkpoint.get(field) != expected for field, expected in checkpoint_expected.items()):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "checkpoint exact binding changed")
    if (isinstance(checkpoint_seq, bool) or not isinstance(checkpoint_seq, int)
            or checkpoint_seq < 1 or checkpoint_seq > len(request["checkpoint_ids"])
            or checkpoint.get("checkpoint_id") != request["checkpoint_ids"][checkpoint_seq - 1]
            or checkpoint_seq != state.get("checkpoint_seq")
            or checkpoint.get("checkpoint_id") != state.get("checkpoint_id")):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "checkpoint sequence/ID position changed")

    bundle_content_hash = _hash_bytes(_json_bytes(bundle))
    if state.get("result_bundle_hash") != bundle_content_hash:
        raise DonorAnalysisWorkerError("RESUME_INVALID", "result Bundle content hash changed")
    spec = SPEC_BY_CAPABILITY[capability]
    producer_expected = {
        "plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": capability,
        "job_id": request["job_id"], "step_id": request["step_id"],
        "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"],
    }
    if (bundle.get("contract_id") != spec.result_contract
            or bundle.get("bundle_type") != spec.bundle_type
            or bundle.get("producer") != producer_expected
            or bundle.get("input_snapshot_hash") != request["run_snapshot_hash"]
            or bundle.get("provenance_receipt_id") != request["provenance_receipt_id"]):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "result Bundle producer/receipt binding changed")
    expected_source_ref = _source_ref(request)
    for item in bundle.get("items", []):
        refs = item.get("source_refs")
        if not isinstance(refs, list) or not refs or refs[0] != expected_source_ref:
            raise DonorAnalysisWorkerError("RESUME_INVALID", "result Bundle source Revision changed")

    expected_stage_key = (
        _derived_id("candidate-stage", context.binding_hash, bundle_content_hash)
        if spec.result_contract == "candidate-batch/v1" else None
    )
    if state.get("candidate_stage_operation_key") != expected_stage_key:
        raise DonorAnalysisWorkerError("RESUME_INVALID", "Candidate stage operation binding changed")
    model_receipt_ids = state.get("model_receipt_ids")
    if (not isinstance(model_receipt_ids, list)
            or len(model_receipt_ids) != len(set(model_receipt_ids))
            or any(not isinstance(item, str) or _ID_RE.fullmatch(item) is None for item in model_receipt_ids)
            or (derived_model_receipt_ids is not None and model_receipt_ids != derived_model_receipt_ids)):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle payload model provenance changed")
    if state.get("skill_chain_result_refs") != bundle.get("skill_chain_result_refs"):
        raise DonorAnalysisWorkerError("RESUME_INVALID", "Bundle Skill provenance changed")

    if receipt is not None:
        receipt_expected = {
            "receipt_id": request["provenance_receipt_id"], "plugin_id": PLUGIN_ID,
            "package_hash": package_hash, "release_id": release_id,
            "capability_id": capability, "job_id": request["job_id"],
            "step_id": request["step_id"], "attempt_id": request["attempt_id"],
            "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"],
            "bundle_id": bundle.get("bundle_id"),
            "bundle_hash": hash_json("result-bundle/v1", bundle),
            "model_receipt_ids": model_receipt_ids,
            "skill_chain_result_refs": bundle.get("skill_chain_result_refs"),
            "created_at": request["created_at"],
        }
        if any(receipt.get(field) != expected for field, expected in receipt_expected.items()):
            raise DonorAnalysisWorkerError("RESUME_INVALID", "provenance receipt exact binding changed")


class DonorAnalysisPlugin:
    """Single-writer worker; all durable effects go through injected Host RPC."""
    plugin_id = PLUGIN_ID
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self.package_hash, self.release_id = _identity()
        self.last_stage_response: dict[str, object] | None = None
        self.last_receipt: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.last_model_receipt_ids: list[str] = []

    def cancel(self, request: Mapping[str, Any]) -> dict[str, object]:
        capability = request.get("capability_id")
        if capability not in {CAPABILITY_ATOM_EXTRACT, CAPABILITY_CLAIM_GENERATE, CAPABILITY_REREVIEW}:
            raise DonorAnalysisWorkerError("INPUT_INVALID", "capability is not cancellable")
        value, context = _context(request, capability)
        return {"accepted": _DISPATCHER.cancel(value["worker_run_id"], context.binding_hash), "worker_run_id": value["worker_run_id"]}

    def atom_extract(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_ATOM_EXTRACT)
        text, nodes = _load_source(value, host); taxonomy, allowed_kinds = _validate_taxonomy(host, value)
        model_request = {"schema": "analysis.book.atom.extract-model-request/v1", "workspace_id": value["workspace_id"],
            "document_id": value["document_id"], "source_revision_id": value["source_revision_id"],
            "canonical_asset_id": value["canonical_asset_id"], "canonical_text_hash": value["canonical_text_hash"],
            "taxonomy_asset_id": value["taxonomy_asset_id"], "taxonomy_asset_hash": value["taxonomy_asset_hash"],
            "taxonomy_id": taxonomy["taxonomy_id"], "taxonomy_version": taxonomy["version"], "nodes": nodes, "output_schema": "analysis.book.atom.extract-model-response/v1"}
        output, receipt_id = _invoke_model(host, value, context, CAPABILITY_ATOM_EXTRACT, model_request)
        self.last_model_receipt_ids = [receipt_id]
        raw_items = _validate_model_envelope(output, "analysis.book.atom.extract-model-response/v1")
        candidates: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_items):
            if raw.get("provenance") is not None:
                raise DonorAnalysisWorkerError("ATOM_INVALID", "model Atom provenance must be null before trusted Host binding")
            bound = deepcopy(raw)
            provenance = _trusted_atom_provenance(
                value, mode="model", analysis_method=bound.get("analysis_method", {}),
                package_hash=self.package_hash, release_id=self.release_id,
                model_receipt_id=receipt_id,
            )
            bound["provenance"] = provenance
            try: atom = validate_atom_payload(bound, canonical_text=text, nodes=nodes, workspace_id=value["workspace_id"],
                document_id=value["document_id"], revision_id=value["source_revision_id"], canonical_text_hash=value["canonical_text_hash"], expected_mode="model", allowed_atom_kinds=allowed_kinds, expected_provenance=provenance)
            except DonorContractError as exc: raise DonorAnalysisWorkerError("ATOM_INVALID", str(exc)) from exc
            data = _json_bytes(atom); asset_id = _upload(host, context, data, "application/json", f"atom-{index}")
            candidates.append(_candidate_item(value, context, atom, asset_id, "book-atom/v1", index))
        return _bundle(value, CAPABILITY_ATOM_EXTRACT, candidates, self.release_id)

    def atom_manual(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_ATOM_MANUAL)
        text, nodes = _load_source(value, host); _taxonomy, allowed_kinds = _validate_taxonomy(host, value)
        _read_asset(host, value["manual_annotation_asset_id"], value["manual_annotation_asset_hash"])
        raw = deepcopy(value["atom_payload"])
        if not isinstance(raw, dict): raise DonorAnalysisWorkerError("ATOM_INVALID", "atom_payload must be an object")
        if raw.get("provenance") is not None:
            raise DonorAnalysisWorkerError("ATOM_INVALID", "manual input provenance must be null; plugin binds the exact Host Asset provenance")
        provenance = _trusted_atom_provenance(
            value, mode="manual", analysis_method=raw.get("analysis_method", {}),
            package_hash=self.package_hash, release_id=self.release_id,
        )
        raw["provenance"] = provenance
        try: atom = validate_atom_payload(raw, canonical_text=text, nodes=nodes, workspace_id=value["workspace_id"],
            document_id=value["document_id"], revision_id=value["source_revision_id"], canonical_text_hash=value["canonical_text_hash"], expected_mode="manual", allowed_atom_kinds=allowed_kinds, expected_provenance=provenance)
        except DonorContractError as exc: raise DonorAnalysisWorkerError("ATOM_INVALID", str(exc)) from exc
        data = _json_bytes(atom); asset_id = _upload(host, context, data, "application/json", "manual-atom")
        return _bundle(value, CAPABILITY_ATOM_MANUAL, [_candidate_item(value, context, atom, asset_id, "book-atom/v1", 0)], self.release_id)

    def claim_generate(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_CLAIM_GENERATE)
        _canonical_text, _nodes, claim_input = _load_claim_authority(value, host)
        model_request = {"schema": "analysis.book.claim.generate-model-request/v1", "workspace_id": value["workspace_id"],
            "document_id": value["document_id"], "source_revision_id": value["source_revision_id"],
            "parameters_asset_id": value["parameters_asset_id"], "parameters_asset_hash": value["parameters_asset_hash"],
            "run_snapshot_hash": value["run_snapshot_hash"],
            "ordered_atoms": claim_input["ordered_atoms"], "output_schema": "analysis.book.claim.generate-model-response/v1"}
        output, receipt_id = _invoke_model(host, value, context, CAPABILITY_CLAIM_GENERATE, model_request)
        self.last_model_receipt_ids = [receipt_id]
        raw_items = _validate_model_envelope(output, "analysis.book.claim.generate-model-response/v1")
        candidates: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_items):
            try: claim = validate_claim_payload(raw, claim_input=claim_input)
            except DonorContractError as exc: raise DonorAnalysisWorkerError("CLAIM_INVALID", str(exc)) from exc
            data = _json_bytes(claim); asset_id = _upload(host, context, data, "application/json", f"claim-{index}")
            candidate = _candidate_item(value, context, claim, asset_id, "book-claim/v1", index)
            # book-claim/v1 intentionally has no plugin provenance field.  Keep
            # the model receipt as a closed Bundle source ref so resume can
            # recover it from immutable Bundle bytes rather than mutable state.
            candidate["source_refs"].append(_model_receipt_ref(value, receipt_id))
            candidates.append(candidate)
        return _bundle(value, CAPABILITY_CLAIM_GENERATE, candidates, self.release_id)

    def rereview(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any]:
        value, context = _context(request, CAPABILITY_REREVIEW)
        text, nodes = _load_source(value, host)
        try: records = validate_rereview_records(value["candidate_records"], source_revision_id=value["source_revision_id"], known_parent_ids=set(value["known_parent_candidate_ids"]))
        except DonorContractError as exc: raise DonorAnalysisWorkerError("REREVIEW_INVALID", str(exc)) from exc
        # Re-read and revalidate every payload against the exact canonical Revision.
        for record in records:
            payload = _strict_json(_read_asset(host, record["payload_asset_id"], record["payload_hash"]))
            try:
                if record["candidate_kind"] == "book_atom":
                    provenance = payload.get("provenance") if isinstance(payload, Mapping) else None
                    mode = provenance.get("mode") if isinstance(provenance, Mapping) else None
                    if mode not in {"model", "manual"}:
                        raise DonorContractError("rereview Atom lacks closed provenance")
                    validate_atom_payload(payload, canonical_text=text, nodes=nodes, workspace_id=value["workspace_id"],
                        document_id=value["document_id"], revision_id=value["source_revision_id"],
                        canonical_text_hash=value["canonical_text_hash"], expected_mode=mode, allowed_atom_kinds=ATOM_KINDS)
                else:
                    if not isinstance(payload, Mapping) or set(payload) != set(CLAIM_FIELDS) or payload.get("schema") != "book-claim/v1" or payload.get("authority") != "candidate_only":
                        raise DonorContractError("rereview Claim payload is not closed")
                    atom_ids = payload.get("ordered_atom_ids")
                    if not isinstance(atom_ids, list) or not atom_ids or len(atom_ids) != len(set(atom_ids)):
                        raise DonorContractError("rereview Claim Atom order is invalid")
                    spans = payload.get("evidence_spans")
                    if not isinstance(spans, list): raise DonorContractError("rereview Claim evidence is not an array")
                    for span in spans:
                        validate_evidence_span(span, text, nodes, workspace_id=value["workspace_id"], document_id=value["document_id"],
                            revision_id=value["source_revision_id"], canonical_text_hash=value["canonical_text_hash"])
            except DonorContractError as exc:
                raise DonorAnalysisWorkerError("REREVIEW_INVALID", str(exc)) from exc
        model_request = {"schema": "analysis.book.rereview-model-request/v1", "workspace_id": value["workspace_id"],
            "document_id": value["document_id"], "source_revision_id": value["source_revision_id"],
            "canonical_asset_id": value["canonical_asset_id"], "canonical_text_hash": value["canonical_text_hash"],
            "nodes": nodes, "candidate_records": records, "allow_successor": value["allow_successor"],
            "output_schema": "analysis.book.rereview-model-response/v1"}
        output, receipt_id = _invoke_model(host, value, context, CAPABILITY_REREVIEW, model_request)
        self.last_model_receipt_ids = [receipt_id]
        raw_items = _validate_model_envelope(output, "analysis.book.rereview-model-response/v1")
        try:
            raw_items = validate_rereview_diagnostics(
                raw_items, candidate_records=records, allow_successor=value["allow_successor"]
            )
        except DonorContractError as exc:
            raise DonorAnalysisWorkerError("MODEL_OUTPUT_INVALID", str(exc)) from exc
        detail_rows: list[dict[str, Any]] = []
        for raw, record in zip(raw_items, records, strict=True):
            successor = raw["successor_candidate_id"]
            details = {"schema": "book-rereview-diagnostic/v1", "candidate_id": record["candidate_id"],
                "recommendation": raw["recommendation"], "successor": None if successor is None else {
                    "mode": "existing_idempotent", "predecessor_candidate_id": record["parent_candidate_id"],
                    "successor_candidate_id": successor},
                "lineage": {
                    "predecessor_candidate_id": record["parent_candidate_id"],
                    "successor_candidate_id": successor,
                }, "mutation_staged": False}
            detail_rows.append(details)
        _validate_rereview_detail_authority(
            records, detail_rows, code="REREVIEW_LINEAGE_INVALID"
        )
        diagnostics: list[dict[str, Any]] = []
        for index, (raw, details) in enumerate(zip(raw_items, detail_rows, strict=True)):
            diagnostics.append(_diagnostic_item(value, context, index, severity=raw["severity"], code=str(raw["code"]),
                message=str(raw["message"]), details=details, host=host))
        return _bundle(value, CAPABILITY_REREVIEW, diagnostics, self.release_id)

    def _finalize(self, request: Mapping[str, Any], capability: str, context: _RunContext,
                  bundle: dict[str, Any], host: HostPort | None, active: _ActiveRun | None) -> dict[str, Any] | None:
        data = _json_bytes(bundle); bundle_hash = _hash_bytes(data)
        bundle_asset_id = _upload(host, context, data, "application/json", "result-bundle")
        _event(host, context, "donor-analysis.result", bundle_asset_id, 2)
        is_async = capability in {CAPABILITY_ATOM_EXTRACT, CAPABILITY_CLAIM_GENERATE, CAPABILITY_REREVIEW}
        if is_async:
            provenance_hash, derived_model_receipts = _bundle_payload_provenance(
                host, request, capability, bundle, self.package_hash, self.release_id,
            )
            if derived_model_receipts is not None and derived_model_receipts != self.last_model_receipt_ids:
                raise DonorAnalysisWorkerError("RESULT_CONTRACT_ERROR", "Bundle payload provenance differs from model receipt")
            state = _state(
                request, context, bundle, bundle_asset_id, bundle_hash, "ready",
                self.last_model_receipt_ids, self.package_hash, self.release_id,
                provenance_hash,
            )
            state_data = _json_bytes(state); state_asset_id = _upload(host, context, state_data, "application/json", "resume-state")
            self.last_checkpoint = _checkpoint(host, request, context, state_asset_id, _hash_bytes(state_data))
            if active is not None and active.cancelled.is_set():
                self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids)
                self.last_stage_response = None
                _complete(host, request, context, "cancelled", None, None, None, 3)
                return None
        stage_key: str | None = None; staged_ids: list[str] = []
        if bundle["contract_id"] == "candidate-batch/v1":
            stage_key, staged_ids = _stage(
                host, request, context, bundle_asset_id, bundle_hash, bundle["items"]
            )
            self.last_stage_response = deepcopy(context.stage_response)
        else:
            self.last_stage_response = None
        self.last_receipt = _receipt(
            request, capability, self.package_hash, self.release_id, bundle, staged_ids,
            self.last_model_receipt_ids, list(bundle.get("skill_chain_result_refs", [])),
        )
        _complete(host, request, context, "succeeded", bundle_asset_id, stage_key, None, 3)
        return bundle

    def _resume(self, request: Mapping[str, Any], capability: str, context: _RunContext,
                host: HostPort | None, active: _ActiveRun) -> dict[str, Any] | None:
        _event(host, context, "donor-analysis.resumed", request["resume_state_asset_id"], 1)
        checkpoint = _load_json_asset(host, request["resume_checkpoint_asset_id"], request["resume_checkpoint_asset_hash"])
        _require_sdk(); assert _verify_checkpoint_sdk is not None
        try: _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"], previous_seq=None)
        except Exception as exc: raise DonorAnalysisWorkerError("RESUME_INVALID", f"checkpoint binding failed: {exc}") from exc
        if checkpoint.get("checkpoint_id") not in request["checkpoint_ids"]:
            raise DonorAnalysisWorkerError("RESUME_INVALID", "checkpoint is not declared by this request")
        state = _load_json_asset(host, request["resume_state_asset_id"], request["resume_state_asset_hash"])
        if not isinstance(state, dict) or set(state) != _RESUME_STATE_FIELDS:
            raise DonorAnalysisWorkerError("RESUME_INVALID", "resume state is not closed")
        bundle = _load_json_asset(host, _id(state["result_bundle_asset_id"], "result_bundle_asset_id"), _hash(state["result_bundle_hash"], "result_bundle_hash"))
        if not isinstance(bundle, dict): raise DonorAnalysisWorkerError("RESUME_INVALID", "resume bundle is not an object")
        _require_sdk(); assert _verify_result_bundle_sdk is not None
        try: _verify_result_bundle_sdk(bundle, snapshot_workspace_id=request["workspace_id"] if bundle.get("contract_id") == "candidate-batch/v1" else None,
            snapshot_hash_value=request["run_snapshot_hash"], known_parent_ids=set(request.get("known_parent_candidate_ids", [])))
        except Exception as exc: raise DonorAnalysisWorkerError("RESUME_INVALID", f"result Bundle binding failed: {exc}") from exc
        provenance_hash, derived_model_receipts = _bundle_payload_provenance(
            host, request, capability, bundle, self.package_hash, self.release_id,
        )
        _validate_resume_binding(
            request, capability, context, checkpoint, state, bundle,
            self.package_hash, self.release_id, provenance_hash,
            derived_model_receipts,
        )
        checkpoint_seq = int(checkpoint["checkpoint_seq"])
        context.last_checkpoint_seq = checkpoint_seq
        model_receipt_ids = list(state["model_receipt_ids"])
        skill_chain_result_refs = list(state["skill_chain_result_refs"])
        bundle_content_hash = _hash_bytes(_json_bytes(bundle))
        _event(host, context, "donor-analysis.result", state["result_bundle_asset_id"], 2)
        if active.cancelled.is_set():
            self.last_receipt = _receipt(
                request, capability, self.package_hash, self.release_id, None, [],
                model_receipt_ids, skill_chain_result_refs,
            )
            _complete(host, request, context, "cancelled", None, None, None, 3); return None
        stage_key: str | None = None; staged_ids: list[str] = []
        if bundle["contract_id"] == "candidate-batch/v1":
            stage_key, staged_ids = _stage(
                host, request, context, state["result_bundle_asset_id"],
                bundle_content_hash, bundle["items"],
            )
        self.last_model_receipt_ids = list(model_receipt_ids)
        self.last_stage_response = deepcopy(context.stage_response)
        self.last_receipt = _receipt(
            request, capability, self.package_hash, self.release_id, bundle, staged_ids,
            model_receipt_ids, skill_chain_result_refs,
        )
        _validate_resume_binding(
            request, capability, context, checkpoint, state, bundle,
            self.package_hash, self.release_id, provenance_hash,
            derived_model_receipts, self.last_receipt,
        )
        _complete(host, request, context, "succeeded", state["result_bundle_asset_id"], stage_key, None, 3)
        return bundle

    def _failure(self, request: Mapping[str, Any], capability: str, context: _RunContext | None,
                 host: HostPort | None, error: BaseException) -> dict[str, Any] | None:
        if isinstance(error, TerminalContractError): raise error
        if context is not None and context.terminal_dispatched:
            raise TerminalContractError(
                "TERMINAL_CONTRACT_ERROR",
                f"terminal {context.terminal_outcome!r} was already dispatched; refusing a second terminal",
            ) from error
        try:
            if context is None:
                for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id"): _id(request.get(field), field)
                _integer(request.get("lease_epoch"), "lease_epoch", 1); _hash(request.get("run_snapshot_hash"), "run_snapshot_hash")
                if not isinstance(request.get("created_at"), str) or _TIME_RE.fullmatch(request["created_at"]) is None:
                    raise DonorAnalysisWorkerError("INPUT_INVALID", "created_at unavailable for terminal failure")
                checkpoints = request.get("checkpoint_ids") if isinstance(request.get("checkpoint_ids"), list) else []
                context = _RunContext(hash_json(capability+"-failure-request/v1", dict(request)), _ZERO_HASH, tuple(checkpoints))
            code = getattr(error, "code", "INTERNAL_ERROR"); message = str(error)[:1024]
            detail = {"schema": "donor-analysis-failure/v1", "code": code, "message": message,
                      "retryable": bool(getattr(error, "retryable", False))}
            item = _diagnostic_item(request, context, 0, severity="error", code=code, message=message,
                                    details=detail, host=host, status="failed", include_source_ref=False)
            bundle = _bundle(request, capability, [item], self.release_id, partial=True)
            data = _json_bytes(bundle); bundle_asset_id = _upload(host, context, data, "application/json", "failure-bundle")
            detail_asset_id = item["details_asset_id"]
            self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, bundle, [], self.last_model_receipt_ids)
            self.last_stage_response = None
            _event(host, context, "donor-analysis.failed", detail_asset_id, context.last_local_seq+1)
            _complete(host, request, context, "failed", bundle_asset_id, None, detail_asset_id, context.last_local_seq+1)
            return bundle
        except TerminalContractError: raise
        except Exception as terminal_error:
            raise DonorAnalysisWorkerError("FAILURE_TERMINAL_ERROR", str(terminal_error)) from terminal_error

    def run(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any] | None:
        value: Mapping[str, Any] = request if isinstance(request, Mapping) else {}
        capability: str | None = None; normalized: dict[str, Any] | None = None
        context: _RunContext | None = None; active: _ActiveRun | None = None
        self.last_model_receipt_ids = []
        try:
            if not isinstance(request, Mapping): raise DonorAnalysisWorkerError("INPUT_INVALID", "request must be an object")
            value = dict(request)
            if "request_asset_id" in value:
                if set(value) != {"request_asset_id", "request_asset_hash"}: raise DonorAnalysisWorkerError("INPUT_INVALID", "request Asset envelope is closed")
                loaded = _load_json_asset(host, _id(value["request_asset_id"], "request_asset_id"), _hash(value["request_asset_hash"], "request_asset_hash"))
                if not isinstance(loaded, dict): raise DonorAnalysisWorkerError("INPUT_INVALID", "request Asset must contain an object")
                value = loaded
            capability = value.get("capability_id")
            if capability in (None, ""):
                capability = {
                    spec.input_schema: spec.capability_id
                    for spec in SPEC_BY_CAPABILITY.values()
                }.get(value.get("schema"))
            if capability not in CAPABILITIES: raise DonorAnalysisWorkerError("CAPABILITY_UNKNOWN", "unknown donor-analysis capability")
            normalized, context = _context(value, capability); operation = normalized["operation"]
            if operation == "cancel": return self.cancel(normalized)
            if operation in {"run", "resume"} and capability != CAPABILITY_ATOM_MANUAL:
                active = _DISPATCHER.begin(normalized["worker_run_id"], context.binding_hash)
            if operation == "resume":
                assert active is not None; return self._resume(normalized, capability, context, host, active)
            if operation == "run": _event(host, context, "donor-analysis.started", None, 1)
            if capability == CAPABILITY_ATOM_EXTRACT: bundle = self.atom_extract(normalized, host)
            elif capability == CAPABILITY_ATOM_MANUAL: bundle = self.atom_manual(normalized, host)
            elif capability == CAPABILITY_CLAIM_GENERATE: bundle = self.claim_generate(normalized, host)
            else: bundle = self.rereview(normalized, host)
            if operation == "validate":
                self.last_stage_response = self.last_receipt = None; return bundle
            return self._finalize(normalized, capability, context, bundle, host, active)
        except TerminalContractError: raise
        except DonorAnalysisWorkerError as exc:
            if capability in CAPABILITIES: return self._failure(normalized or value, capability, context, host, exc)
            return None
        except Exception as exc:
            if capability in CAPABILITIES: return self._failure(normalized or value, capability, context, host, DonorAnalysisWorkerError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"))
            return None
        finally:
            if active is not None: _DISPATCHER.finish(active)


def _descriptor(capability_id: str, release_id: str) -> dict[str, Any]:
    try: return SPEC_BY_CAPABILITY[capability_id].descriptor(release_id)
    except KeyError as exc:
        raise DonorAnalysisWorkerError(
            "CAPABILITY_UNKNOWN", "unknown donor-analysis capability"
        ) from exc


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    _, release_id = _identity(); return _descriptor(capability_id or CAPABILITY_ATOM_EXTRACT, release_id)


PACKAGE_HASH, RELEASE_ID = _identity()
DESCRIPTORS = descriptors(RELEASE_ID)
_RUNTIME = DonorAnalysisPlugin()


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> dict[str, Any] | None:
    if request is None: return capability_descriptor()
    return _RUNTIME.run(request, host)


__all__ = ["CAPABILITIES", "CAPABILITY_ATOM_EXTRACT", "CAPABILITY_ATOM_MANUAL",
           "CAPABILITY_CLAIM_GENERATE", "CAPABILITY_REREVIEW", "DESCRIPTORS", "DonorAnalysisPlugin",
           "DonorAnalysisWorkerError", "HostPort", "NEEDS", "PACKAGE_HASH", "PLUGIN_ID", "RELEASE_ID",
           "TerminalContractError", "VERSION", "capability_descriptor", "main"]
