"""Host-bound narrative worker; Core remains the sole authority."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
from typing import Any, Mapping, Protocol, Sequence

from .capability_spec import CAPABILITIES, NEEDS, PLUGIN_ID, SPEC_BY_CAPABILITY, VERSION, descriptors
from .contract import (
    NarrativeContractError, canonical_json_bytes, compile_narrative_plan, hash_json,
    interpret_plot_template,
    sha256_text, validate_narrative_plan, validate_narrative_synthesis,
    validate_narrative_unit, validate_nodes,
)
from .package_identity import load_runtime_identity

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes_sdk
    from plotpilot_plugin_sdk.verifier import (
        assert_valid as _assert_valid_sdk,
        validate_rpc_result as _validate_rpc_result_sdk,
        verify_checkpoint as _verify_checkpoint_sdk,
        verify_provenance_receipt as _verify_provenance_receipt_sdk,
        verify_result_bundle as _verify_result_bundle_sdk,
    )
except ImportError as exc:  # pragma: no cover
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _canonical_bytes_sdk = _assert_valid_sdk = _validate_rpc_result_sdk = None
    _verify_checkpoint_sdk = _verify_provenance_receipt_sdk = None
    _verify_result_bundle_sdk = None
else:
    _SDK_IMPORT_ERROR = None

CAPABILITY_UNIT_EXTRACT = "analysis.narrative.unit.extract/v1"
CAPABILITY_PLAN_COMPILE = "analysis.narrative.plan.compile/v1"
CAPABILITY_SYNTHESIZE = "analysis.narrative.synthesize/v1"
_COMMON_REQUIRED = frozenset({
    "schema", "capability_id", "operation_key", "operation", "job_id", "step_id",
    "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids",
    "provenance_receipt_id", "created_at", "total_units", "run_snapshot_hash",
    "workspace_id", "document_id", "source_revision_id",
})
_SOURCE_REQUIRED = frozenset({"canonical_asset_id", "canonical_text_hash", "nodes"})
_UNIT_REQUIRED = _SOURCE_REQUIRED | frozenset({
    "taxonomy_asset_id", "taxonomy_asset_hash", "plot_template_asset_id",
    "plot_template_asset_hash", "model_profile_revision_id",
})
_PLAN_REQUIRED = _SOURCE_REQUIRED | frozenset({
    "narrative_unit_assets", "template_asset_id", "template_asset_hash",
    "template_binding", "plan_id", "plan_version", "hierarchy",
})
_SYNTH_REQUIRED = _SOURCE_REQUIRED | frozenset({
    "plan_asset_id", "plan_asset_hash", "source_bindings", "model_profile_revision_id",
})
_OPTIONAL = frozenset({"known_parent_candidate_ids", "skill_chain_result_refs", "parent_receipt_ids", "broker_invocations"})
_RESUME = frozenset({"resume_checkpoint_asset_id", "resume_checkpoint_asset_hash", "resume_state_asset_id", "resume_state_asset_hash"})
_RAW_AUTHORITY = frozenset({"path", "file_path", "url", "database", "router", "sqlite"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$")
_PAGE_SIZE = 8_388_608
_MAX_PAGES = 4096
_DATA_PLUGIN_ID = "com.plotpilot.novelagent.plot-structure-template"
_DATA_FORMAT_ID = "plot-structure-template/v1"
_DATA_VERSION = "1.0.0"
# The Data package is intentionally a separate immutable package.  The code
# wheel therefore carries the release binding as frozen constants rather than
# reaching back into a checkout at runtime.  The canonical digest accepts
# either raw or JCS-normalized Host Assets while rejecting every semantic
# replacement (beat, purpose, constraints, mappings, or template set).
_DATA_PACKAGE_HASH = "fa96f117e9ce134c05911c377615c2cdeb2719f99914e329f0140773d80a2c06"
_DATA_RELEASE_ID = "cabac87f1ed3ea74a6766093c0a9f3a912e38957fc9037ec6c3df1d5c238a3e3"
_TEMPLATE_CANONICAL_HASH = "434c008b059e6c34b4c9b5840f3cea776f7ad226a7ed355427f7a0b2b963a107"
_TEMPLATE_ID = "general-longform-seven-beat"


class NarrativeAnalysisWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TerminalContractError(NarrativeAnalysisWorkerError):
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
    stage_response: dict[str, object] | None = None


@dataclass
class _ActiveRun:
    worker_run_id: str
    binding_hash: str
    cancelled: threading.Event
    child_jobs: list[str] = field(default_factory=list)
    child_propagate_cancel: dict[str, bool] = field(default_factory=dict)


class _Dispatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: _ActiveRun | None = None

    def begin(self, worker_run_id: str, binding_hash: str) -> _ActiveRun:
        with self._lock:
            if self._active is not None:
                raise NarrativeAnalysisWorkerError("WORKER_BUSY", "narrative-analysis already has an active operation", retryable=True)
            self._active = _ActiveRun(worker_run_id, binding_hash, threading.Event())
            return self._active

    def cancel(self, worker_run_id: str, binding_hash: str) -> _ActiveRun | None:
        with self._lock:
            if self._active is None or self._active.worker_run_id != worker_run_id or self._active.binding_hash != binding_hash:
                return None
            self._active.cancelled.set()
            return self._active

    def finish(self, active: _ActiveRun) -> None:
        with self._lock:
            if self._active is active:
                self._active = None


_DISPATCHER = _Dispatcher()


def _require_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or any(x is None for x in (_canonical_bytes_sdk, _assert_valid_sdk, _validate_rpc_result_sdk, _verify_checkpoint_sdk, _verify_provenance_receipt_sdk, _verify_result_bundle_sdk)):
        raise NarrativeAnalysisWorkerError("SDK_UNAVAILABLE", "public PlotPilot SDK unavailable") from _SDK_IMPORT_ERROR


def _identity() -> tuple[str, str]:
    try:
        value = load_runtime_identity()
        return str(value["package_hash"]), str(value["release_id"])
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"{label} must be Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"{label} must be integer >= {minimum}")
    return value


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    _require_sdk()
    assert _canonical_bytes_sdk is not None
    try:
        return bytes(_canonical_bytes_sdk(value))
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("CANONICALIZATION_ERROR", str(exc)) from exc


def _strict_json(raw: bytes, *, code: str = "ASSET_READ_ERROR") -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    try:
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except Exception as exc:
        raise NarrativeAnalysisWorkerError(code, "Asset is not strict UTF-8 JSON") from exc


def _host_call(host: HostPort | None, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    if host is None or not hasattr(host, "call"):
        raise NarrativeAnalysisWorkerError("HOST_REQUIRED", "Core HostPort.call required")
    request = dict(params)
    try:
        response = host.call(method, request)
    except NarrativeAnalysisWorkerError:
        raise
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}", retryable=True) from exc
    if not isinstance(response, Mapping):
        raise NarrativeAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} returned non-object")
    assert _validate_rpc_result_sdk is not None
    try:
        _validate_rpc_result_sdk(method, dict(response), request={"method": method, "params": request})
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} result invalid: {exc}") from exc
    return dict(response)


def _read_asset(host: HostPort | None, asset_id: str, expected_hash: str | None) -> bytes:
    _id(asset_id, "asset_id")
    if expected_hash is not None:
        _hash(expected_hash, "expected_hash")
    chunks: list[bytes] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        response = _host_call(host, "host.asset.read/v1", {"asset_id": asset_id, "offset": offset, "length": _PAGE_SIZE})
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "base64_chunk absent")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "invalid base64") from exc
        if response.get("content_hash") != _hash_bytes(chunk):
            raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "page hash mismatch")
        chunks.append(chunk)
        next_offset = response.get("next_offset")
        if next_offset is None:
            break
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset != offset + len(chunk) or next_offset <= offset:
            raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "non-contiguous page")
        offset = next_offset
    else:
        raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "page limit exceeded")
    data = b"".join(chunks)
    if expected_hash is not None and _hash_bytes(data) != expected_hash:
        raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "Asset hash mismatch")
    return data


def _load_json_asset(host: HostPort | None, asset_id: str, expected_hash: str | None) -> Any:
    return _load_json_asset_with_raw(host, asset_id, expected_hash)[0]


def _load_json_asset_with_raw(host: HostPort | None, asset_id: str, expected_hash: str | None) -> tuple[Any, bytes]:
    """Read strict JSON and retain the exact immutable Asset bytes.

    Template validation needs the bytes in addition to the decoded object: a
    caller-controlled expected hash must never be allowed to become the
    template's identity authority.
    """
    raw = _read_asset(host, asset_id, expected_hash)
    return _strict_json(raw), raw


def _derived_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{hashlib.sha256((prefix + chr(10) + chr(10).join(parts)).encode()).hexdigest()[:48]}"


def _binding_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in request.items() if key not in _RESUME}
    result["operation"] = "run"
    return result


def _request_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-request/v1", dict(request))


def _binding_hash(request: Mapping[str, Any], capability: str) -> str:
    return hash_json(capability + "-binding/v1", _binding_projection(request))


def _result_request_hash(request: Mapping[str, Any], capability: str) -> str:
    return _request_hash(_binding_projection(request), capability)


def _required_for(capability: str) -> frozenset[str]:
    return {CAPABILITY_UNIT_EXTRACT: _UNIT_REQUIRED, CAPABILITY_PLAN_COMPILE: _PLAN_REQUIRED, CAPABILITY_SYNTHESIZE: _SYNTH_REQUIRED}[capability]


def _validate_context(request: Mapping[str, Any], capability: str) -> dict[str, Any]:
    value = dict(request)
    if any(key in value for key in _RAW_AUTHORITY):
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", "raw path/text/database authority fields forbidden")
    spec = SPEC_BY_CAPABILITY[capability]
    operation = value.get("operation")
    if operation not in spec.supports:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"operation {operation!r} not declared")
    required = set(_COMMON_REQUIRED | _required_for(capability))
    allowed = required | set(_OPTIONAL)
    if operation == "resume":
        required |= set(_RESUME)
        allowed |= set(_RESUME)
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"request fields are not closed: missing={sorted(required-set(value))}, extra={sorted(set(value)-allowed)}")
    if value["schema"] != spec.input_schema or value["capability_id"] != capability or value["operation_key"] != capability:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", "schema/capability/operation_key mismatch")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id", "document_id", "source_revision_id"):
        _id(value[field], field)
    _integer(value["lease_epoch"], "lease_epoch", 1)
    _integer(value["total_units"], "total_units", 1)
    _hash(value["run_snapshot_hash"], "run_snapshot_hash")
    if not isinstance(value["created_at"], str) or _TIME_RE.fullmatch(value["created_at"]) is None:
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", "created_at invalid")
    checkpoints = value["checkpoint_ids"]
    if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) != len(set(checkpoints)):
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", "checkpoint_ids invalid")
    [_id(item, "checkpoint_id") for item in checkpoints]
    for field in ("known_parent_candidate_ids", "parent_receipt_ids"):
        if field in value:
            if not isinstance(value[field], list) or len(value[field]) != len(set(value[field])):
                raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"{field} invalid")
            [_id(item, field) for item in value[field]]
    if "skill_chain_result_refs" in value and not isinstance(value["skill_chain_result_refs"], list):
        raise NarrativeAnalysisWorkerError("INPUT_INVALID", "skill_chain_result_refs invalid")
    if capability in {CAPABILITY_UNIT_EXTRACT, CAPABILITY_PLAN_COMPILE, CAPABILITY_SYNTHESIZE}:
        _id(value["canonical_asset_id"], "canonical_asset_id")
        _hash(value["canonical_text_hash"], "canonical_text_hash")
        if not isinstance(value["nodes"], list):
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "nodes invalid")
    if capability == CAPABILITY_UNIT_EXTRACT:
        for field in ("taxonomy_asset_id", "plot_template_asset_id", "model_profile_revision_id"):
            _id(value[field], field)
        for field in ("taxonomy_asset_hash", "plot_template_asset_hash"):
            _hash(value[field], field)
    elif capability == CAPABILITY_PLAN_COMPILE:
        for field in ("template_asset_id", "plan_id"):
            _id(value[field], field)
        _hash(value["template_asset_hash"], "template_asset_hash")
        _integer(value["plan_version"], "plan_version", 1)
        if not isinstance(value["narrative_unit_assets"], list) or not value["narrative_unit_assets"]:
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "narrative_unit_assets invalid")
        if not isinstance(value["template_binding"], Mapping) or not isinstance(value["hierarchy"], list):
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "template_binding/hierarchy invalid")
    else:
        for field in ("plan_asset_id", "model_profile_revision_id"):
            _id(value[field], field)
        _hash(value["plan_asset_hash"], "plan_asset_hash")
        if not isinstance(value["source_bindings"], list) or not value["source_bindings"]:
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "source_bindings invalid")
        if "broker_invocations" in value and not isinstance(value["broker_invocations"], list):
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "broker_invocations invalid")
    if operation == "resume":
        for field in ("resume_checkpoint_asset_id", "resume_state_asset_id"):
            _id(value[field], field)
        for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"):
            _hash(value[field], field)
    return value


def _context(request: Mapping[str, Any], capability: str) -> tuple[dict[str, Any], _RunContext]:
    value = _validate_context(request, capability)
    return value, _RunContext(_request_hash(value, capability), _binding_hash(value, capability), tuple(value["checkpoint_ids"]))


def _upload(host: HostPort | None, context: _RunContext, data: bytes, mime: str, suffix: str, *, operation_key: str | None = None, upload_id: str | None = None) -> str:
    expected = _hash_bytes(data)
    op = operation_key or context.request_hash
    uid = upload_id or context.request_hash + "-upload-" + suffix
    chunks = [b""] if not data else [data[index:index + _PAGE_SIZE] for index in range(0, len(data), _PAGE_SIZE)]
    accepted = 0
    asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _host_call(host, "host.asset.create/v1", {"operation_key": op, "upload_id": uid, "offset": accepted, "mime": mime, "total_size": len(data), "expected_hash": expected, "chunk_hash": _hash_bytes(chunk), "base64_chunk": base64.b64encode(chunk).decode("ascii"), "final": final})
        if response.get("upload_id") != uid or response.get("accepted_bytes") != accepted + len(chunk) or response.get("completed") is not final:
            raise NarrativeAnalysisWorkerError("ASSET_CREATE_ERROR", "upload acknowledgement mismatch")
        raw = response.get("asset_id")
        if final:
            asset_id = _id(raw, "asset_id")
        elif raw is not None:
            raise NarrativeAnalysisWorkerError("ASSET_CREATE_ERROR", "non-final Asset ID")
        accepted += len(chunk)
    status = _host_call(host, "host.asset.upload.status/v1", {"upload_id": uid, "expected_hash": expected})
    if status.get("accepted_bytes") != len(data) or status.get("completed") is not True or status.get("asset_id") != asset_id:
        raise NarrativeAnalysisWorkerError("ASSET_UPLOAD_ERROR", "upload status mismatch")
    assert asset_id is not None
    return asset_id


def _record_event(context: _RunContext, response: Mapping[str, object], method: str) -> None:
    if response.get("accepted") is not True:
        raise NarrativeAnalysisWorkerError("HOST_REJECTED", f"Core rejected {method}")
    seq = response.get("job_event_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= context.last_job_event_seq:
        raise NarrativeAnalysisWorkerError("HOST_CONTRACT_ERROR", f"{method} sequence not monotonic")
    context.last_job_event_seq = seq


def _event(host: HostPort | None, context: _RunContext, event_type: str, payload_asset_id: str | None, local_seq: int) -> None:
    response = _host_call(host, "host.job.event/v1", {"operation_key": context.request_hash, "event_type": event_type, "payload_asset_id": payload_asset_id, "local_seq": local_seq})
    _record_event(context, response, "host.job.event/v1")
    context.last_local_seq = local_seq


def _source_ref(request: Mapping[str, Any]) -> dict[str, Any]:
    return {"workspace_id": request["workspace_id"], "source_type": "canonical_revision", "source_id": request["document_id"], "revision_or_hash": request["source_revision_id"]}


def _candidate_item(request: Mapping[str, Any], payload: Mapping[str, Any], payload_asset_id: str, payload_schema: str, ordinal: int, source_refs: Sequence[Mapping[str, Any]] | None = None, *, status: str = "complete") -> dict[str, Any]:
    payload_hash = _hash_bytes(_json_bytes(payload))
    item_id = _derived_id("candidate", _result_request_hash(request, request["capability_id"]), str(ordinal), payload_hash)
    entity_id = "narrative:" + item_id.split(":", 1)[1]
    base_hash = request.get("canonical_text_hash") or request.get("plan_asset_hash") or request.get("template_asset_hash")
    assert isinstance(base_hash, str)
    return {"schema": "candidate-item/v1", "item_id": item_id, "item_kind": "relation_set", "target": {"workspace_id": request["workspace_id"], "entity_kind": "relation_set", "entity_id": entity_id}, "mutation": {"mode": "relation_patch", "payload_schema": payload_schema, "payload_hash": payload_hash}, "payload_asset_id": payload_asset_id, "base": {"revision_id": request["source_revision_id"], "content_hash": base_hash}, "write_set": [{"workspace_id": request["workspace_id"], "entity_kind": "relation_set", "entity_id": entity_id, "revision_id": request["source_revision_id"], "content_hash": base_hash}], "parent_candidate_ids": list(request.get("known_parent_candidate_ids", [])), "source_refs": [dict(item) for item in (source_refs or [_source_ref(request)])], "status": status}


def _producer(request: Mapping[str, Any], capability: str, release_id: str) -> dict[str, Any]:
    return {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": capability, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"]}


def _bundle(request: Mapping[str, Any], capability: str, items: list[dict[str, Any]], release_id: str, *, partial: bool = False, diagnostic: bool = False) -> dict[str, Any]:
    spec = SPEC_BY_CAPABILITY[capability]
    contract, bundle_type = (("diagnostic-bundle/v1", "diagnostic") if diagnostic else (spec.result_contract, spec.bundle_type))
    bundle = {"schema": "result-bundle/v1", "contract_id": contract, "bundle_id": _derived_id("bundle", _result_request_hash(request, capability), contract), "bundle_type": bundle_type, "producer": _producer(request, capability, release_id), "input_snapshot_hash": request["run_snapshot_hash"], "items": items, "warnings": [], "partial": partial, "provenance_receipt_id": request["provenance_receipt_id"], "skill_chain_result_refs": deepcopy(list(request.get("skill_chain_result_refs", [])))}
    _require_sdk()
    assert _verify_result_bundle_sdk is not None
    try:
        _verify_result_bundle_sdk(bundle, snapshot_workspace_id=request.get("workspace_id"), snapshot_hash_value=request["run_snapshot_hash"], known_parent_ids=set(request.get("known_parent_candidate_ids", [])), attempt_state="failed" if diagnostic else None)
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return bundle


def _stage(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, bundle_asset_id: str, bundle_hash: str, items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    stage_key = _derived_id("candidate-stage", context.binding_hash, bundle_hash)
    response = _host_call(host, "host.candidate.stage/v1", {"operation_key": stage_key, "result_bundle_asset_id": bundle_asset_id, "input_snapshot_hash": request["run_snapshot_hash"]})
    rows = response.get("staged_items")
    expected = [item["item_id"] for item in items]
    if response.get("accepted") is not True or not isinstance(rows, list) or len(rows) != len(expected):
        raise NarrativeAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage rejected/cardinality mismatch")
    actual: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "candidate_id", "stage_status", "publication_eligibility"}:
            raise NarrativeAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage row not closed")
        actual.append(_id(row["item_id"], "item_id"))
        _id(row["candidate_id"], "candidate_id")
    if actual != expected:
        raise NarrativeAnalysisWorkerError("CANDIDATE_STAGE_ERROR", "stage order mismatch")
    context.stage_response = deepcopy(response)
    return stage_key, actual


def _receipt(request: Mapping[str, Any], capability: str, package_hash: str, release_id: str, bundle: Mapping[str, Any] | None, staged: list[str], models: list[str], parents: Sequence[str]) -> dict[str, Any]:
    receipt = {"schema": "provenance-receipt/v1", "receipt_id": request["provenance_receipt_id"], "plugin_id": PLUGIN_ID, "release_id": release_id, "package_hash": package_hash, "capability_id": capability, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "bundle_id": None if bundle is None else bundle["bundle_id"], "bundle_hash": None if bundle is None else hash_json("result-bundle/v1", bundle), "parent_receipt_ids": list(dict.fromkeys(parents)), "model_receipt_ids": list(dict.fromkeys(models)), "skill_chain_result_refs": deepcopy(list(request.get("skill_chain_result_refs", []))), "staged_items": list(dict.fromkeys(staged)), "created_at": request["created_at"]}
    receipt["receipt_hash"] = hash_json("provenance-receipt/v1", receipt)
    _require_sdk()
    assert _verify_provenance_receipt_sdk is not None
    try:
        _verify_provenance_receipt_sdk(receipt)
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("RECEIPT_CONTRACT_ERROR", str(exc)) from exc
    return receipt


def _complete(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, outcome: str, bundle_asset_id: str | None, stage_key: str | None, detail_asset_id: str | None, local_seq: int, *, active: _ActiveRun | None = None) -> None:
    if context.terminal_dispatched:
        raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED", "terminal dispatched twice")
    context.terminal_dispatched = True
    context.terminal_outcome = outcome
    response = _host_call(host, "host.job.complete/v1", {"operation_key": context.request_hash, "worker_run_id": request["worker_run_id"], "outcome": outcome, "result_bundle_asset_id": bundle_asset_id, "candidate_stage_operation_key": stage_key, "terminal_detail_asset_id": detail_asset_id, "local_seq": local_seq})
    expected = {"succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled"}[outcome]
    if response.get("attempt_state") != expected or response.get("step_state") != expected or response.get("job_state") != expected or response.get("provenance_receipt_id") != request["provenance_receipt_id"]:
        raise TerminalContractError("TERMINAL_CONTRACT_ERROR", "terminal acknowledgement mismatch")
    _record_event(context, response, "host.job.complete/v1")


def _checkpoint(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, state_asset_id: str, state_hash: str) -> dict[str, Any]:
    if context.last_checkpoint_seq >= len(context.checkpoint_ids):
        raise NarrativeAnalysisWorkerError("CHECKPOINT_ERROR", "checkpoint budget exhausted")
    checkpoint = {"schema": "checkpoint/v1", "checkpoint_id": context.checkpoint_ids[context.last_checkpoint_seq], "checkpoint_seq": context.last_checkpoint_seq + 1, "job_id": request["job_id"], "step_id": request["step_id"], "source_attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "replay_policy": "checkpoint_resume", "completed_units": request["total_units"], "total_units": request["total_units"], "unit_set_hash": state_hash, "state_asset_id": state_asset_id, "created_at": request["created_at"]}
    checkpoint["checkpoint_hash"] = hash_json("checkpoint/v1", checkpoint)
    assert _verify_checkpoint_sdk is not None
    try:
        _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"], previous_seq=None)
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("CHECKPOINT_ERROR", str(exc)) from exc
    data = _json_bytes(checkpoint)
    asset_id = _upload(host, context, data, "application/json", "checkpoint")
    response = _host_call(host, "host.checkpoint.commit/v1", {"operation_key": context.request_hash + "-checkpoint", "checkpoint_asset_id": asset_id})
    _record_event(context, response, "host.checkpoint.commit/v1")
    if response.get("checkpoint_id") != checkpoint["checkpoint_id"] or response.get("completed_units") != checkpoint["completed_units"] or response.get("total_units") != checkpoint["total_units"]:
        raise NarrativeAnalysisWorkerError("CHECKPOINT_ERROR", "checkpoint acknowledgement binding mismatch")
    context.last_checkpoint_seq += 1
    return {"checkpoint": checkpoint, "checkpoint_asset_id": asset_id, "checkpoint_asset_hash": _hash_bytes(data), "state_asset_id": state_asset_id, "state_asset_hash": state_hash}


def _invoke_model(host: HostPort | None, request: Mapping[str, Any], context: _RunContext, capability: str, model_request: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    # Every model-backed narrative operation must carry the exact canonical
    # Revision text that was read and hashed above.  Keeping this assertion in
    # the common upload path prevents a future caller from accidentally
    # replacing the text with only an Asset ID/hash (which a generic provider
    # cannot dereference) or with a transformed copy.
    if capability in {CAPABILITY_UNIT_EXTRACT, CAPABILITY_SYNTHESIZE}:
        canonical_text = model_request.get("canonical_text")
        expected_hash = model_request.get("canonical_text_hash")
        if not isinstance(canonical_text, str) or canonical_text == "" or sha256_text(canonical_text) != request["canonical_text_hash"]:
            raise NarrativeAnalysisWorkerError("MODEL_REQUEST_INVALID", "model request does not contain exact canonical text")
        if expected_hash != request["canonical_text_hash"]:
            raise NarrativeAnalysisWorkerError("MODEL_REQUEST_INVALID", "model request canonical hash is not bound")
    data = _json_bytes(model_request)
    asset_id = _upload(host, context, data, "application/json", "model-request")
    # Re-read the persisted request Asset before invoking the provider.  The
    # upload acknowledgement alone proves only that the Host accepted bytes;
    # an altered/replayed Asset ID must not reach a model while the worker
    # still believes it sent the canonical Revision.  Byte equality also
    # protects Unicode scalar text and its exact canonical encoding.
    persisted = _read_asset(host, asset_id, _hash_bytes(data))
    if persisted != data:
        raise NarrativeAnalysisWorkerError("MODEL_REQUEST_INVALID", "persisted model request differs from canonical request")
    response = _host_call(host, "host.model.invoke/v1", {"operation_key": context.request_hash + "-model", "invocation_id": _derived_id("invocation", context.request_hash), "invocation_key": _derived_id("model-key", context.binding_hash, capability), "model_profile_revision_id": request["model_profile_revision_id"], "request_asset_id": asset_id, "replay_policy": "manual_if_unknown"})
    if response.get("state") != "received" or response.get("response_asset_id") is None or response.get("uncertainty") is not None:
        raise NarrativeAnalysisWorkerError("MODEL_INVOKE_ERROR", "model response uncertain", retryable=True)
    receipt = _id(response.get("receipt_id"), "model receipt")
    output = _load_json_asset(host, _id(response["response_asset_id"], "response_asset_id"), None)
    if not isinstance(output, dict):
        raise NarrativeAnalysisWorkerError("MODEL_OUTPUT_INVALID", "model output non-object")
    return output, receipt


def _load_source(request: Mapping[str, Any], host: HostPort | None) -> tuple[str, list[dict[str, Any]]]:
    raw = _read_asset(host, request["canonical_asset_id"], request["canonical_text_hash"])
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "canonical Asset non-UTF8") from exc
    if sha256_text(text) != request["canonical_text_hash"]:
        raise NarrativeAnalysisWorkerError("ASSET_READ_ERROR", "canonical hash mismatch")
    try:
        nodes = validate_nodes(request["nodes"], len(text))
    except NarrativeContractError as exc:
        raise NarrativeAnalysisWorkerError("EVIDENCE_INVALID", str(exc)) from exc
    return text, nodes


def _source_refs_from_attributions(attrs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for attr in attrs:
        ref = {"workspace_id": attr["workspace_id"], "source_type": attr["source_type"], "source_id": attr["source_id"], "revision_or_hash": attr["revision_or_hash"]}
        key = tuple(ref.values())
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _validate_template_asset(raw: Any, binding: Mapping[str, Any] | None = None, *, raw_bytes: bytes | None = None) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"schema", "format_id", "template_set_id", "version", "templates", "interpreter_mappings", "mergeable"}:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "plot template root is not closed")
    if raw["schema"] != _DATA_FORMAT_ID or raw["format_id"] != _DATA_FORMAT_ID or raw["template_set_id"] != _DATA_PLUGIN_ID or raw["version"] != _DATA_VERSION or raw["mergeable"] is not False:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "plot template identity/policy differs")
    if not isinstance(raw["templates"], list) or not raw["templates"] or not isinstance(raw["interpreter_mappings"], list):
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "plot template arrays are invalid")
    # The request supplies an Asset/hash pair, but that pair is not an
    # authority.  Bind the decoded bytes to the frozen Data release digest so
    # installed wheels (which do not contain the separate Data directory) are
    # just as strict as a source checkout.  JCS normalization makes this
    # compatible with Host Assets while any semantic edit changes the digest.
    try:
        if raw_bytes is not None and _strict_json(raw_bytes, code="TEMPLATE_INVALID") != raw:
            raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template Asset decoding differs from supplied value")
        canonical_hash = _hash_bytes(_json_bytes(raw))
    except NarrativeAnalysisWorkerError:
        raise
    except Exception as exc:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template Asset cannot be canonicalized") from exc
    if canonical_hash != _TEMPLATE_CANONICAL_HASH:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template bytes differ from the frozen Data release")
    expected_binding = {
        "data_plugin_id": _DATA_PLUGIN_ID,
        "data_release_id": _DATA_RELEASE_ID,
        "package_hash": _DATA_PACKAGE_HASH,
        "format_id": _DATA_FORMAT_ID,
        "template_id": _TEMPLATE_ID,
    }
    if binding is None:
        # Unit extraction has no public binding field; the frozen identity is
        # still enforced by the constants above and this selected template.
        binding = expected_binding
    if not isinstance(binding, Mapping) or set(binding) != set(expected_binding):
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template binding is not closed")
    if dict(binding) != expected_binding:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template Data release identity differs")
    selected: Mapping[str, Any] | None = None
    template_ids: set[str] = set()
    for item in raw["templates"]:
        if not isinstance(item, Mapping) or set(item) != {"template_id", "display_name", "levels", "beats", "constraints"}:
            raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template entry is not closed")
        template_id = _id(item["template_id"], "template_id")
        if template_id in template_ids:
            raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "duplicate template identity")
        template_ids.add(template_id)
        if item["template_id"] == binding["template_id"]:
            selected = item
    if selected is None:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "requested template is absent")
    if selected["levels"] != ["book", "volume", "chapter", "plot_unit"]:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template levels are not canonical")
    constraints = selected["constraints"]
    if not isinstance(constraints, Mapping) or set(constraints) != {"ordered", "allow_optional_beats", "requires_evidence_closure", "authority"} or constraints["ordered"] is not True or constraints["requires_evidence_closure"] is not True or constraints["authority"] != "template_only":
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template constraints are invalid")
    beats = selected["beats"]
    if not isinstance(beats, list) or not beats:
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template beats are absent")
    beat_orders: list[int] = []
    beat_ids: set[str] = set()
    for beat in beats:
        if not isinstance(beat, Mapping) or set(beat) != {"beat_id", "label_zh", "order", "target_level", "purpose", "required"} or beat["target_level"] not in {"book", "volume", "chapter", "plot_unit"} or not isinstance(beat["required"], bool) or isinstance(beat["order"], bool) or not isinstance(beat["order"], int):
            raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template beat is invalid")
        beat_id = _id(beat["beat_id"], "beat_id")
        if beat_id in beat_ids:
            raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "duplicate beat identity")
        beat_ids.add(beat_id)
        beat_orders.append(beat["order"])
    if beat_orders != sorted(set(beat_orders)):
        raise NarrativeAnalysisWorkerError("TEMPLATE_INVALID", "template beat ordering is invalid")
    return selected


class NarrativeAnalysisPlugin:
    plugin_id = PLUGIN_ID
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self.package_hash, self.release_id = _identity()
        self.last_stage_response: dict[str, Any] | None = None
        self.last_receipt: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.last_model_receipt_ids: list[str] = []
        self.last_child_receipt_ids: list[str] = []

    def cancel(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, object]:
        capability = request["capability_id"]
        if capability not in {CAPABILITY_UNIT_EXTRACT, CAPABILITY_SYNTHESIZE}:
            raise NarrativeAnalysisWorkerError("INPUT_INVALID", "capability not cancellable")
        active = _DISPATCHER.cancel(request["worker_run_id"], _binding_hash(request, capability))
        if active is not None:
            for child_job_id in active.child_jobs:
                if not active.child_propagate_cancel.get(child_job_id, True):
                    continue
                response = _host_call(host, "host.capability.cancel/v1", {"operation_key": _derived_id("child-cancel", active.binding_hash, child_job_id), "child_job_id": child_job_id, "reason": "parent narrative run cancelled"})
                if response.get("accepted") is not True:
                    raise NarrativeAnalysisWorkerError("BROKER_CANCEL_ERROR", "Core rejected propagated child cancel")
        return {"accepted": active is not None, "worker_run_id": request["worker_run_id"]}

    def unit_extract(self, request: Mapping[str, Any], context: _RunContext, host: HostPort | None) -> dict[str, Any]:
        text, nodes = _load_source(request, host)
        taxonomy = _load_json_asset(host, request["taxonomy_asset_id"], request["taxonomy_asset_hash"])
        template, template_bytes = _load_json_asset_with_raw(host, request["plot_template_asset_id"], request["plot_template_asset_hash"])
        _validate_template_asset(template, raw_bytes=template_bytes)
        model_request = {"schema": "analysis.narrative.unit.extract-model-request/v1", "workspace_id": request["workspace_id"], "document_id": request["document_id"], "source_revision_id": request["source_revision_id"], "canonical_text_hash": request["canonical_text_hash"], "canonical_text": text, "nodes": nodes, "taxonomy": taxonomy, "plot_structure_template": template, "output_schema": "analysis.narrative.unit.extract-model-response/v1"}
        output, receipt = _invoke_model(host, request, context, CAPABILITY_UNIT_EXTRACT, model_request)
        self.last_model_receipt_ids = [receipt]
        if output.get("schema") != "analysis.narrative.unit.extract-model-response/v1" or not isinstance(output.get("items"), list) or not output["items"]:
            raise NarrativeAnalysisWorkerError("MODEL_OUTPUT_INVALID", "unit model envelope invalid")
        items: list[dict[str, Any]] = []
        for index, raw in enumerate(output["items"]):
            if not isinstance(raw, Mapping):
                raise NarrativeAnalysisWorkerError("MODEL_OUTPUT_INVALID", "unit non-object")
            value = deepcopy(dict(raw))
            value["provenance"] = {"source_mode": "model", "model_receipt_id": receipt}
            try:
                unit = validate_narrative_unit(value, canonical_text=text, nodes=nodes, workspace_id=request["workspace_id"], document_id=request["document_id"], revision_id=request["source_revision_id"], canonical_text_hash=request["canonical_text_hash"], require_source=True)
            except NarrativeContractError as exc:
                raise NarrativeAnalysisWorkerError("NARRATIVE_UNIT_INVALID", str(exc)) from exc
            data = _json_bytes(unit)
            asset_id = _upload(host, context, data, "application/json", f"narrative-unit-{index}")
            items.append(_candidate_item(request, unit, asset_id, "narrative-unit/v1", index, _source_refs_from_attributions(unit["source_attributions"])))
        return _bundle(request, CAPABILITY_UNIT_EXTRACT, items, self.release_id)

    def plan_compile(self, request: Mapping[str, Any], context: _RunContext, host: HostPort | None) -> dict[str, Any]:
        text, nodes = _load_source(request, host)
        template_raw, template_bytes = _load_json_asset_with_raw(host, request["template_asset_id"], request["template_asset_hash"])
        selected_template = _validate_template_asset(template_raw, request["template_binding"], raw_bytes=template_bytes)
        refs: list[dict[str, Any]] = []
        units: list[dict[str, Any]] = []
        source_refs: list[dict[str, Any]] = []
        for index, binding in enumerate(request["narrative_unit_assets"]):
            if not isinstance(binding, Mapping) or set(binding) != {"asset_id", "asset_hash"}:
                raise NarrativeAnalysisWorkerError("INPUT_INVALID", f"narrative_unit_assets[{index}] not closed")
            asset_id = _id(binding["asset_id"], "unit asset_id")
            asset_hash = _hash(binding["asset_hash"], "unit asset_hash")
            raw = _load_json_asset(host, asset_id, asset_hash)
            try:
                unit = validate_narrative_unit(raw, canonical_text=text, nodes=nodes, workspace_id=request["workspace_id"], document_id=request["document_id"], revision_id=request["source_revision_id"], canonical_text_hash=request["canonical_text_hash"], require_source=True)
            except NarrativeContractError as exc:
                raise NarrativeAnalysisWorkerError("NARRATIVE_UNIT_INVALID", str(exc)) from exc
            if _hash_bytes(_json_bytes(unit)) != asset_hash:
                raise NarrativeAnalysisWorkerError("NARRATIVE_UNIT_INVALID", "unit Asset hash differs after canonical validation")
            units.append(unit)
            span_ids = [span["evidence_span_id"] for span in unit["evidence_spans"]]
            attr_ids = [attr["attribution_id"] for attr in unit["source_attributions"]]
            refs.append({"unit_id": unit["unit_id"], "payload_asset_id": asset_id, "payload_hash": asset_hash, "order": unit["order"], "evidence_span_ids": span_ids, "source_attribution_ids": attr_ids})
            source_refs.extend(_source_refs_from_attributions(unit["source_attributions"]))
        # The Data package is executable input, not a decorative binding.  Use
        # the selected immutable template to interpret required beat levels,
        # ordering and evidence constraints before compiling the business plan.
        try:
            interpret_plot_template(selected_template, request["hierarchy"], units)
        except NarrativeContractError as exc:
            raise NarrativeAnalysisWorkerError("NARRATIVE_PLAN_INVALID", f"template interpretation failed: {exc}") from exc
        try:
            plan = compile_narrative_plan(plan_id=request["plan_id"], version=request["plan_version"], template_binding=request["template_binding"], unit_refs=refs, hierarchy=request["hierarchy"], run_snapshot_hash=request["run_snapshot_hash"])
        except NarrativeContractError as exc:
            raise NarrativeAnalysisWorkerError("NARRATIVE_PLAN_INVALID", str(exc)) from exc
        data = _json_bytes(plan)
        asset_id = _upload(host, context, data, "application/json", "narrative-plan")
        return _bundle(request, CAPABILITY_PLAN_COMPILE, [_candidate_item(request, plan, asset_id, "narrative-plan/v1", 0, source_refs)], self.release_id)

    def _poll_child(self, request: Mapping[str, Any], context: _RunContext, host: HostPort | None, child_job_id: str) -> dict[str, Any]:
        after = 0
        for _ in range(64):
            response = _host_call(host, "host.capability.poll/v1", {"child_job_id": child_job_id, "after_job_event_seq": after})
            next_seq = response.get("next_job_event_seq")
            if not isinstance(next_seq, int) or next_seq < after:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child poll sequence moved backwards")
            if response.get("terminal") is True:
                if response.get("result_bundle_asset_id") is None or response.get("provenance_receipt_id") is None:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "terminal child poll lacks Result/receipt")
                return dict(response)
            if next_seq == after:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "non-terminal child poll made no progress", retryable=True)
            after = next_seq
        raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child poll exceeded bounded attempts", retryable=True)

    def _broker_sources(self, request: Mapping[str, Any], context: _RunContext, host: HostPort | None, active: _ActiveRun, plan: Mapping[str, Any], text: str, nodes: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
        live: dict[str, dict[str, Any]] = {}
        invoked_bindings: set[str] = set()
        for index, invocation in enumerate(request.get("broker_invocations", [])):
            invocation_fields = {"binding_id", "input_asset_id", "parameters_asset_id", "expected_result_contract", "propagate_cancel"}
            if not isinstance(invocation, Mapping) or set(invocation) != invocation_fields:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", f"broker_invocations[{index}] not closed")
            binding_id = _id(invocation["binding_id"], "binding_id")
            if binding_id in invoked_bindings:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "duplicate broker binding invocation")
            invoked_bindings.add(binding_id)
            input_asset_id = _id(invocation["input_asset_id"], "input_asset_id")
            # The compact runtime request carries only the two public Asset IDs;
            # durable broker-invocation records below bind their exact hashes.
            # Do not silently accept a null parameters ID through the private
            # _id helper; the Host RPC contract explicitly permits null.
            parameters_asset_id = invocation["parameters_asset_id"]
            if parameters_asset_id is not None:
                parameters_asset_id = _id(parameters_asset_id, "parameters_asset_id")
            propagate = invocation["propagate_cancel"]
            if not isinstance(propagate, bool):
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "propagate_cancel must be boolean")
            if invocation["expected_result_contract"] not in {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "expected_result_contract is invalid")
            response = _host_call(host, "host.capability.invoke/v1", {"operation_key": _derived_id("broker-invoke", context.binding_hash, str(index)), "binding_id": binding_id, "input_asset_id": input_asset_id, "parameters_asset_id": parameters_asset_id, "expected_result_contract": invocation["expected_result_contract"], "propagate_cancel": propagate})
            if response.get("accepted") is not True:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child invocation rejected")
            child_job_id = _id(response.get("child_job_id"), "child_job_id")
            active.child_jobs.append(child_job_id)
            active.child_propagate_cancel[child_job_id] = propagate
            polled = self._poll_child(request, context, host, child_job_id)
            live[binding_id] = {
                "child_job_id": child_job_id,
                "child_step_id": response.get("child_step_id"),
                "child_run_snapshot_asset_id": response.get("child_run_snapshot_asset_id"),
                "child_run_snapshot_hash": response.get("child_run_snapshot_hash"),
                "child_result_contract": response.get("child_result_contract"),
                "propagate_cancel": propagate,
                "input_asset_id": input_asset_id,
                "parameters_asset_id": parameters_asset_id,
                "expected_result_contract": invocation["expected_result_contract"],
                "result_bundle_asset_id": polled.get("result_bundle_asset_id"),
                "provenance_receipt_id": polled.get("provenance_receipt_id"),
            }
        units: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        source_refs: list[dict[str, Any]] = []
        plan_ref_by_id = {ref["unit_id"]: ref for ref in plan["unit_refs"]}
        seen_units: set[str] = set()
        partial_required = False
        required_child_fields = {"child_job_id", "parent_job_id", "parent_step_id", "parent_attempt_id", "invoke_operation_key", "binding_id", "broker_invocation_asset_id", "broker_invocation_hash", "child_run_snapshot_asset_id", "child_run_snapshot_hash", "result_contract", "required", "propagate_cancel", "state", "result_bundle_asset_id", "provenance_receipt_id"}
        binding_fields = {"binding_id", "broker_invocation_asset_id", "broker_invocation_hash", "child_record_asset_id", "child_record_asset_hash", "result_bundle_asset_id", "result_bundle_hash"}
        for index, binding in enumerate(request["source_bindings"]):
            if not isinstance(binding, Mapping) or set(binding) != binding_fields:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", f"source_bindings[{index}] not closed")
            binding_id = _id(binding["binding_id"], "binding_id")
            invocation = _load_json_asset(host, _id(binding["broker_invocation_asset_id"], "broker_invocation_asset_id"), _hash(binding["broker_invocation_hash"], "broker_invocation_hash"))
            child = _load_json_asset(host, _id(binding["child_record_asset_id"], "child_record_asset_id"), _hash(binding["child_record_asset_hash"], "child_record_asset_hash"))
            if not isinstance(invocation, Mapping) or not isinstance(child, Mapping) or not required_child_fields.issubset(child) or set(child) - required_child_fields:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "Broker invocation/child record invalid")
            _require_sdk()
            assert _assert_valid_sdk is not None
            try:
                _assert_valid_sdk("broker-invocation/v1", invocation)
                _assert_valid_sdk("broker-child-record/v1", child)
            except Exception as exc:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", f"Broker invocation/child record invalid: {exc}") from exc
            if child["parent_job_id"] != request["job_id"] or child["parent_step_id"] != request["step_id"] or child["parent_attempt_id"] != request["attempt_id"] or child["binding_id"] != binding_id or invocation.get("binding_id") != binding_id:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child parent/binding changed")
            if invocation.get("parent_job_id") != request["job_id"] or invocation.get("parent_step_id") != request["step_id"] or invocation.get("parent_attempt_id") != request["attempt_id"] or invocation.get("propagate_cancel") != child["propagate_cancel"] or invocation.get("expected_result_contract") != child["result_contract"]:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "invocation/child policy changed")
            if invocation.get("invoke_operation_key") != child["invoke_operation_key"]:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "invocation operation identity changed")
            if binding_id in live:
                live_invocation = live[binding_id]
                if (live_invocation.get("input_asset_id") != invocation.get("input_asset_id")
                        or live_invocation.get("parameters_asset_id") != invocation.get("parameters_asset_id")
                        or live_invocation.get("expected_result_contract") != invocation.get("expected_result_contract")):
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "live invocation binding changed")
            try:
                _read_asset(host, invocation["input_asset_id"], invocation["input_hash"])
                if invocation["parameters_asset_id"] is None:
                    if invocation["parameters_hash"] is not None:
                        raise NarrativeAnalysisWorkerError("BROKER_INVALID", "null parameters Asset has a hash")
                else:
                    _read_asset(host, invocation["parameters_asset_id"], invocation["parameters_hash"])
            except NarrativeAnalysisWorkerError:
                raise
            except Exception as exc:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "broker invocation Asset closure is invalid") from exc
            if child["broker_invocation_asset_id"] != binding["broker_invocation_asset_id"] or child["broker_invocation_hash"] != binding["broker_invocation_hash"] or child["result_bundle_asset_id"] != binding["result_bundle_asset_id"]:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child Asset binding changed")
            if not isinstance(child["required"], bool) or not isinstance(child["propagate_cancel"], bool):
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child required/propagate_cancel invalid")
            child_job_id = _id(child["child_job_id"], "child_job_id")
            if binding_id in live and live[binding_id]["child_job_id"] != child_job_id:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "live child identity differs from durable record")
            receipt = _id(child["provenance_receipt_id"], "child provenance_receipt_id")
            if child["state"] not in {"succeeded", "partial"}:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "required child not terminal-success")
            bundle_asset_id = _id(binding["result_bundle_asset_id"], "result_bundle_asset_id")
            bundle_hash = _hash(binding["result_bundle_hash"], "result_bundle_hash")
            if binding_id in live:
                live_child = live[binding_id]
                if (live_child.get("result_bundle_asset_id") != bundle_asset_id
                        or live_child.get("provenance_receipt_id") != receipt
                        or live_child.get("child_run_snapshot_hash") != child["child_run_snapshot_hash"]
                        or live_child.get("child_run_snapshot_asset_id") != child["child_run_snapshot_asset_id"]
                        or live_child.get("child_result_contract") != child["result_contract"]
                        or live_child.get("propagate_cancel") != child["propagate_cancel"]):
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "polled child result differs from durable record")
            bundle = _load_json_asset(host, bundle_asset_id, bundle_hash)
            if not isinstance(bundle, Mapping) or bundle.get("schema") != "result-bundle/v1" or bundle.get("contract_id") != child["result_contract"] or bundle.get("provenance_receipt_id") != receipt or bundle.get("input_snapshot_hash") != child["child_run_snapshot_hash"]:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child Result/receipt/snapshot mismatch")
            _require_sdk()
            assert _verify_result_bundle_sdk is not None
            try:
                _verify_result_bundle_sdk(bundle, snapshot_workspace_id=request["workspace_id"], snapshot_hash_value=child["child_run_snapshot_hash"], known_parent_ids=set(request.get("known_parent_candidate_ids", [])))
            except Exception as exc:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", f"child Result invalid: {exc}") from exc
            # The durable child state is only a claim.  Derive finality from
            # the verified Result Bundle and reject any disagreement instead
            # of promoting a partial child to a complete parent result.
            statuses = bundle.get("items")
            if not isinstance(statuses, list):
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child Result items are invalid")
            bundle_partial = bundle.get("partial") is True
            has_incomplete_item = any(
                isinstance(item, Mapping) and item.get("status") in {"partial", "failed", "skipped"}
                for item in statuses
            )
            verified_partial = bundle_partial or has_incomplete_item
            if (child["state"] == "partial") != verified_partial:
                raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child state disagrees with verified Result Bundle")
            if child["required"] and verified_partial:
                partial_required = True
            child_view = {"binding_id": binding_id, "child_job_id": child_job_id, "child_run_snapshot_hash": child["child_run_snapshot_hash"], "result_contract": child["result_contract"], "result_bundle_asset_id": bundle_asset_id, "result_bundle_hash": bundle_hash, "provenance_receipt_id": receipt, "broker_invocation_asset_id": binding["broker_invocation_asset_id"], "broker_invocation_hash": binding["broker_invocation_hash"]}
            children.append(child_view)
            self.last_child_receipt_ids.append(receipt)
            for item in bundle.get("items", []):
                if not isinstance(item, Mapping) or item.get("schema") != "candidate-item/v1" or item.get("status") not in {"complete", "partial"}:
                    continue
                mutation = item.get("mutation")
                if not isinstance(mutation, Mapping) or not isinstance(mutation.get("payload_hash"), str):
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child candidate mutation is invalid")
                payload_asset_id = _id(item.get("payload_asset_id"), "payload_asset_id")
                payload_hash = _hash(mutation["payload_hash"], "payload_hash")
                payload = _load_json_asset(host, payload_asset_id, payload_hash)
                try:
                    unit = validate_narrative_unit(payload, canonical_text=text, nodes=nodes, workspace_id=request["workspace_id"], document_id=request["document_id"], revision_id=request["source_revision_id"], canonical_text_hash=request["canonical_text_hash"], require_source=True)
                except NarrativeContractError as exc:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", str(exc)) from exc
                if _hash_bytes(_json_bytes(unit)) != payload_hash:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child Unit hash differs")
                unit_id = unit["unit_id"]
                expected_ref = plan_ref_by_id.get(unit_id)
                if expected_ref is None or expected_ref["payload_asset_id"] != payload_asset_id or expected_ref["payload_hash"] != payload_hash:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "child Unit is not bound to plan ref")
                if unit_id in seen_units:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "duplicate/conflicting Unit identity")
                seen_units.add(unit_id)
                units.append(unit)
                source_refs.extend(_source_refs_from_attributions(unit["source_attributions"]))
        durable_bindings = {child_item["binding_id"] for child_item in children}
        # A fresh synthesis run is only source-closed when every durable
        # binding has a corresponding live invocation that was polled to a
        # terminal response.  Allowing a non-empty ``source_bindings`` list
        # without an invocation would turn an unverified child record into a
        # trusted parent input.  (Resume reuses its verified parent Bundle
        # through the separate checkpoint path and never enters this helper.)
        if invoked_bindings != durable_bindings:
            raise NarrativeAnalysisWorkerError("BROKER_INVALID", "live Broker bindings do not close over durable records")
        if set(plan_ref_by_id) != seen_units or not children:
            raise NarrativeAnalysisWorkerError("BROKER_INVALID", "synthesis source closure does not match plan Units")
        return units, children, source_refs, partial_required

    def synthesize(self, request: Mapping[str, Any], context: _RunContext, host: HostPort | None, active: _ActiveRun) -> dict[str, Any]:
        text, nodes = _load_source(request, host)
        plan_raw = _load_json_asset(host, request["plan_asset_id"], request["plan_asset_hash"])
        try:
            plan = validate_narrative_plan(plan_raw)
        except NarrativeContractError as exc:
            raise NarrativeAnalysisWorkerError("NARRATIVE_PLAN_INVALID", str(exc)) from exc
        units, children, source_refs, partial_required = self._broker_sources(request, context, host, active, plan, text, nodes)
        spans: list[dict[str, Any]] = []
        attrs: list[dict[str, Any]] = []
        span_map: dict[str, dict[str, Any]] = {}
        attr_map: dict[str, dict[str, Any]] = {}
        for unit in units:
            for span in unit["evidence_spans"]:
                sid = span["evidence_span_id"]
                if sid in span_map and span_map[sid] != span:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "conflicting EvidenceSpan identity")
                if sid not in span_map:
                    span_map[sid] = deepcopy(span)
                    spans.append(deepcopy(span))
            for attr in unit["source_attributions"]:
                aid = attr["attribution_id"]
                if aid in attr_map and attr_map[aid] != attr:
                    raise NarrativeAnalysisWorkerError("BROKER_INVALID", "conflicting attribution identity")
                if aid not in attr_map:
                    attr_map[aid] = deepcopy(attr)
                    attrs.append(deepcopy(attr))
        model_request = {"schema": "analysis.narrative.synthesize-model-request/v1", "workspace_id": request["workspace_id"], "document_id": request["document_id"], "source_revision_id": request["source_revision_id"], "canonical_text_hash": request["canonical_text_hash"], "canonical_text": text, "nodes": [dict(node) for node in nodes], "plan": plan, "units": units, "broker_children": children, "output_schema": "analysis.narrative.synthesize-model-response/v1"}
        output, receipt = _invoke_model(host, request, context, CAPABILITY_SYNTHESIZE, model_request)
        self.last_model_receipt_ids = [receipt]
        if output.get("schema") != "analysis.narrative.synthesize-model-response/v1" or not isinstance(output.get("synthesis"), Mapping):
            raise NarrativeAnalysisWorkerError("MODEL_OUTPUT_INVALID", "synthesis model envelope invalid")
        raw = dict(output["synthesis"])
        raw.update({"schema": "narrative-synthesis/v1", "plan_ref": {"plan_id": plan["plan_id"], "payload_asset_id": request["plan_asset_id"], "payload_hash": request["plan_asset_hash"]}, "evidence_spans": spans, "source_attributions": attrs, "broker_children": children})
        try:
            synthesis = validate_narrative_synthesis(raw, expected_plan=plan)
        except NarrativeContractError as exc:
            raise NarrativeAnalysisWorkerError("SYNTHESIS_INVALID", str(exc)) from exc
        data = _json_bytes(synthesis)
        asset_id = _upload(host, context, data, "application/json", "narrative-synthesis")
        status = "partial" if partial_required else "complete"
        return _bundle(request, CAPABILITY_SYNTHESIZE, [_candidate_item(request, synthesis, asset_id, "narrative-synthesis/v1", 0, source_refs, status=status)], self.release_id, partial=partial_required)

    def _finalize(self, request: Mapping[str, Any], capability: str, context: _RunContext, bundle: dict[str, Any], host: HostPort | None, active: _ActiveRun | None) -> dict[str, Any] | None:
        data = _json_bytes(bundle)
        bundle_hash = _hash_bytes(data)
        bundle_asset_id = _upload(host, context, data, "application/json", "result-bundle", operation_key=_derived_id("result-upload-operation", context.binding_hash), upload_id=_derived_id("result-upload", context.binding_hash))
        _event(host, context, "narrative-analysis.result", bundle_asset_id, 2)
        if capability in {CAPABILITY_UNIT_EXTRACT, CAPABILITY_SYNTHESIZE}:
            state = {"schema": "narrative-analysis-resume-state/v1", "binding_hash": context.binding_hash, "request_hash": context.request_hash, "package_hash": self.package_hash, "release_id": self.release_id, "capability_id": capability, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "result_bundle_asset_id": bundle_asset_id, "result_bundle_hash": bundle_hash, "model_receipt_ids": self.last_model_receipt_ids, "child_receipt_ids": self.last_child_receipt_ids}
            state_data = _json_bytes(state)
            state_asset_id = _upload(host, context, state_data, "application/json", "resume-state")
            self.last_checkpoint = _checkpoint(host, request, context, state_asset_id, _hash_bytes(state_data))
            if active is not None and active.cancelled.is_set():
                self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
                _complete(host, request, context, "cancelled", None, None, state_asset_id, 3)
                return None
        if active is not None and active.cancelled.is_set():
            self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
            _complete(host, request, context, "cancelled", None, None, None, context.last_local_seq + 1)
            return None
        stage_key, staged = _stage(host, request, context, bundle_asset_id, bundle_hash, bundle["items"])
        self.last_stage_response = context.stage_response
        if active is not None and active.cancelled.is_set():
            self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
            _complete(host, request, context, "cancelled", None, None, None, context.last_local_seq + 1)
            return None
        self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, bundle, staged, self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
        _complete(host, request, context, "succeeded", bundle_asset_id, stage_key, None, context.last_local_seq + 1, active=active)
        return bundle

    def _resume(self, request: Mapping[str, Any], capability: str, context: _RunContext, host: HostPort | None, active: _ActiveRun) -> dict[str, Any] | None:
        # Resume is still a request for the same immutable inputs.  Re-read and
        # validate the plot Data Asset for unit extraction so a Host cannot
        # silently replace/tamper the template between the original run and a
        # resumed stage, even though no model call is made on this path.
        if capability == CAPABILITY_UNIT_EXTRACT:
            template, template_bytes = _load_json_asset_with_raw(
                host, request["plot_template_asset_id"], request["plot_template_asset_hash"]
            )
            _validate_template_asset(template, raw_bytes=template_bytes)
        checkpoint = _load_json_asset(host, request["resume_checkpoint_asset_id"], request["resume_checkpoint_asset_hash"])
        state = _load_json_asset(host, request["resume_state_asset_id"], request["resume_state_asset_hash"])
        assert _verify_checkpoint_sdk is not None
        try:
            _verify_checkpoint_sdk(checkpoint, expected_snapshot_hash=request["run_snapshot_hash"], previous_seq=None)
        except Exception as exc:
            raise NarrativeAnalysisWorkerError("RESUME_INVALID", f"checkpoint: {exc}") from exc
        expected_state = {"schema", "binding_hash", "request_hash", "package_hash", "release_id", "capability_id", "job_id", "step_id", "attempt_id", "lease_epoch", "run_snapshot_hash", "result_bundle_asset_id", "result_bundle_hash", "model_receipt_ids", "child_receipt_ids"}
        expected_original_request_hash = _request_hash(_binding_projection(request), capability)
        if not isinstance(state, Mapping) or set(state) != expected_state or state["schema"] != "narrative-analysis-resume-state/v1" or state["binding_hash"] != context.binding_hash or state["request_hash"] != expected_original_request_hash or state["package_hash"] != self.package_hash or state["release_id"] != self.release_id or state["capability_id"] != capability or state["job_id"] != request["job_id"] or state["step_id"] != request["step_id"] or state["attempt_id"] != request["attempt_id"] or state["lease_epoch"] != request["lease_epoch"] or state["run_snapshot_hash"] != request["run_snapshot_hash"]:
            raise NarrativeAnalysisWorkerError("RESUME_INVALID", "resume state identity changed")
        checkpoint_id = checkpoint.get("checkpoint_id")
        if (checkpoint_id not in context.checkpoint_ids
                or checkpoint.get("checkpoint_seq") != context.checkpoint_ids.index(checkpoint_id) + 1
                or checkpoint.get("completed_units") != request["total_units"]
                or checkpoint.get("total_units") != request["total_units"]
                or checkpoint.get("state_asset_id") != request["resume_state_asset_id"]
                or checkpoint.get("unit_set_hash") != request["resume_state_asset_hash"]
                or checkpoint.get("job_id") != request["job_id"]
                or checkpoint.get("step_id") != request["step_id"]
                or checkpoint.get("source_attempt_id") != request["attempt_id"]
                or checkpoint.get("lease_epoch") != request["lease_epoch"]):
            raise NarrativeAnalysisWorkerError("RESUME_INVALID", "checkpoint/state binding changed")
        for field in ("model_receipt_ids", "child_receipt_ids"):
            if (not isinstance(state[field], list) or len(state[field]) != len(set(state[field]))):
                raise NarrativeAnalysisWorkerError("RESUME_INVALID", f"resume {field} are not closed")
            [_id(item, field) for item in state[field]]
        result_raw = _read_asset(host, _id(state["result_bundle_asset_id"], "result_bundle_asset_id"), _hash(state["result_bundle_hash"], "result_bundle_hash"))
        bundle = _strict_json(result_raw, code="RESUME_INVALID")
        assert _verify_result_bundle_sdk is not None
        try:
            _verify_result_bundle_sdk(bundle, snapshot_workspace_id=request["workspace_id"], snapshot_hash_value=request["run_snapshot_hash"], known_parent_ids=set(request.get("known_parent_candidate_ids", [])))
        except Exception as exc:
            raise NarrativeAnalysisWorkerError("RESUME_INVALID", f"Bundle: {exc}") from exc
        if _hash_bytes(result_raw) != state["result_bundle_hash"]:
            raise NarrativeAnalysisWorkerError("RESUME_INVALID", "resume Result hash changed")
        self.last_model_receipt_ids = list(state["model_receipt_ids"])
        self.last_child_receipt_ids = list(state["child_receipt_ids"])
        _event(host, context, "narrative-analysis.resumed", request["resume_state_asset_id"], 1)
        if active.cancelled.is_set():
            self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids, self.last_child_receipt_ids)
            _complete(host, request, context, "cancelled", None, None, request["resume_state_asset_id"], 2)
            return None
        stage_key, staged = _stage(host, request, context, state["result_bundle_asset_id"], state["result_bundle_hash"], bundle["items"])
        self.last_stage_response = context.stage_response
        if active.cancelled.is_set():
            self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, None, [], self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
            _complete(host, request, context, "cancelled", None, None, request["resume_state_asset_id"], 2)
            return None
        self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, bundle, staged, self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
        _complete(host, request, context, "succeeded", state["result_bundle_asset_id"], stage_key, None, 2, active=active)
        return bundle

    def _failure(self, request: Mapping[str, Any], capability: str | None, context: _RunContext | None, host: HostPort | None, error: BaseException) -> dict[str, Any] | None:
        if isinstance(error, TerminalContractError):
            raise error
        if context is not None and context.terminal_dispatched:
            raise TerminalContractError("TERMINAL_CONTRACT_ERROR", f"terminal {context.terminal_outcome!r} was already dispatched; refusing a second terminal") from error
        if context is None or capability not in CAPABILITIES:
            return None
        code = getattr(error, "code", "INTERNAL_ERROR")
        message = str(error)[:1024]
        details = {"schema": "narrative-analysis-failure/v1", "code": code, "message": message, "retryable": bool(getattr(error, "retryable", False))}
        data = _json_bytes(details)
        detail_asset_id = _upload(host, context, data, "application/json", "failure-detail")
        item = {"schema": "diagnostic-item/v1", "item_id": _derived_id("diagnostic", context.request_hash, code), "severity": "error", "code": code, "message": message, "details_asset_id": detail_asset_id, "details_hash": _hash_bytes(data), "source_refs": [], "status": "failed"}
        bundle = _bundle(request, capability, [item], self.release_id, partial=True, diagnostic=True)
        bundle_data = _json_bytes(bundle)
        bundle_asset_id = _upload(host, context, bundle_data, "application/json", "failure-bundle")
        self.last_receipt = _receipt(request, capability, self.package_hash, self.release_id, bundle, [], self.last_model_receipt_ids, [*request.get("parent_receipt_ids", []), *self.last_child_receipt_ids])
        self.last_stage_response = None
        seq = context.last_local_seq + 1
        _event(host, context, "narrative-analysis.failed", detail_asset_id, seq)
        _complete(host, request, context, "failed", bundle_asset_id, None, detail_asset_id, seq + 1)
        return bundle

    def run(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any] | None:
        value = dict(request) if isinstance(request, Mapping) else {}
        capability: str | None = None
        context: _RunContext | None = None
        active: _ActiveRun | None = None
        self.last_model_receipt_ids = []
        self.last_child_receipt_ids = []
        try:
            if not isinstance(request, Mapping):
                raise NarrativeAnalysisWorkerError("INPUT_INVALID", "request must be object")
            if "request_asset_id" in value:
                if set(value) != {"request_asset_id", "request_asset_hash"}:
                    raise NarrativeAnalysisWorkerError("INPUT_INVALID", "request Asset envelope not closed")
                value = _load_json_asset(host, _id(value["request_asset_id"], "request_asset_id"), _hash(value["request_asset_hash"], "request_asset_hash"))
                if not isinstance(value, dict):
                    raise NarrativeAnalysisWorkerError("INPUT_INVALID", "request Asset non-object")
            capability = value.get("capability_id") or {spec.input_schema: spec.capability_id for spec in SPEC_BY_CAPABILITY.values()}.get(value.get("schema"))
            if capability not in CAPABILITIES:
                raise NarrativeAnalysisWorkerError("CAPABILITY_UNKNOWN", "unknown narrative capability")
            value, context = _context(value, capability)
            operation = value["operation"]
            if operation == "cancel":
                return self.cancel(value, host)
            if operation in {"run", "resume"} and capability != CAPABILITY_PLAN_COMPILE:
                active = _DISPATCHER.begin(value["worker_run_id"], context.binding_hash)
            if operation == "resume":
                assert active is not None
                return self._resume(value, capability, context, host, active)
            if operation == "run":
                _event(host, context, "narrative-analysis.started", None, 1)
            if capability == CAPABILITY_UNIT_EXTRACT:
                bundle = self.unit_extract(value, context, host)
            elif capability == CAPABILITY_PLAN_COMPILE:
                bundle = self.plan_compile(value, context, host)
            else:
                assert active is not None
                bundle = self.synthesize(value, context, host, active)
            if operation == "validate":
                self.last_stage_response = None
                self.last_receipt = None
                return bundle
            return self._finalize(value, capability, context, bundle, host, active)
        except TerminalContractError:
            raise
        except NarrativeAnalysisWorkerError as exc:
            return self._failure(value, capability, context, host, exc)
        except Exception as exc:
            return self._failure(value, capability, context, host, NarrativeAnalysisWorkerError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"))
        finally:
            if active is not None:
                _DISPATCHER.finish(active)


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    _, release_id = _identity()
    capability = capability_id or CAPABILITY_UNIT_EXTRACT
    try:
        return SPEC_BY_CAPABILITY[capability].descriptor(release_id)
    except KeyError as exc:
        raise NarrativeAnalysisWorkerError("CAPABILITY_UNKNOWN", "unknown narrative capability") from exc


PACKAGE_HASH, RELEASE_ID = _identity()
DESCRIPTORS = descriptors(RELEASE_ID)
_RUNTIME = NarrativeAnalysisPlugin()


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> dict[str, Any] | None:
    if request is None:
        return capability_descriptor()
    return _RUNTIME.run(request, host)


__all__ = ["CAPABILITIES", "CAPABILITY_PLAN_COMPILE", "CAPABILITY_SYNTHESIZE", "CAPABILITY_UNIT_EXTRACT", "DESCRIPTORS", "HostPort", "NEEDS", "NarrativeAnalysisPlugin", "NarrativeAnalysisWorkerError", "PACKAGE_HASH", "PLUGIN_ID", "RELEASE_ID", "TerminalContractError", "VERSION", "capability_descriptor", "main"]
