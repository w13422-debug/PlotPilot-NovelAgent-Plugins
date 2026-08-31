"""Host-bound, deterministic outline projection worker.

The projection is deliberately a read-only view over immutable Narrative
Assets.  All durable effects (Assets, checkpoints, receipts and terminal job
state) are made through the public HostPort and every result is checked with
the public SDK verifier before it is published.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Protocol, Sequence

from .capability_spec import CAPABILITY_ID, INPUT_SCHEMA, OUTPUT_SCHEMA, PLUGIN_ID, RESULT_CONTRACT, descriptor
from .package_identity import load_runtime_identity

try:  # The SDK is a public dependency of every installed Code Plugin.
    from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs
    from plotpilot_plugin_sdk.verifier import (
        verify_checkpoint,
        verify_provenance_receipt,
        verify_result_bundle,
        verify_attempt_result,
    )
except ImportError as exc:  # pragma: no cover - exercised only by a broken install
    canonical_bytes = hash_jcs = None  # type: ignore[assignment]
    verify_checkpoint = verify_provenance_receipt = verify_result_bundle = verify_attempt_result = None  # type: ignore[assignment]
    _SDK_IMPORT_ERROR: BaseException | None = exc
else:
    _SDK_IMPORT_ERROR = None


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIME = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)
_VIEWS = ("tree", "card", "timeline", "relation")
_LEVELS = ("book", "volume", "chapter", "plot_unit")
_LEVEL_INDEX = {name: index for index, name in enumerate(_LEVELS)}
_CHILD_CONTRACTS = {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}
_DATA_PLUGIN_ID = "com.plotpilot.novelagent.plot-structure-template"
_SOURCE_RECEIPT_FIELDS = frozenset({"source_receipt_id", "source_receipt_asset_id", "source_receipt_asset_hash"})
_PAGE_SIZE = 8_388_608
_MAX_PAGES = 4096


class HostPort(Protocol):
    def call(self, method: str, params: dict[str, object]) -> Mapping[str, object]: ...


class ProjectionError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TerminalContractError(ProjectionError):
    """Raised when a terminal ACK is malformed or a second fence is attempted."""


@dataclass
class _Context:
    request: dict[str, Any]
    request_hash: str
    binding_hash: str
    checkpoint_ids: tuple[str, ...]
    last_event_seq: int = 0
    last_local_seq: int = 0
    checkpoint_seq: int = 0
    terminal: bool = False
    terminal_outcome: str | None = None
    # Set only after the immediate Narrative synthesis receipt has been
    # verified and bound to the exact source Bundle.  Failure/cancel paths
    # must never propagate an unverified caller-supplied parent ID.
    source_receipt: dict[str, Any] | None = None


@dataclass(frozen=True)
class _NarrativeParentClosure:
    receipt: dict[str, Any]
    result_bundle: dict[str, Any]
    synthesis: dict[str, Any]
    synthesis_asset_id: str
    synthesis_asset_hash: str


@dataclass
class _Active:
    worker_run_id: str
    binding_hash: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    context: _Context | None = None


class _Dispatcher:
    """Module-lifetime dispatcher so ``main(cancel)`` reaches a running call."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, _Active] = {}

    def begin(self, worker_run_id: str, binding_hash: str) -> _Active:
        with self._lock:
            if self._active:
                raise ProjectionError("WORKER_BUSY", "outline projection already has an active run", retryable=True)
            active = _Active(worker_run_id, binding_hash)
            self._active[worker_run_id] = active
            return active

    def cancel(self, worker_run_id: str, binding_hash: str) -> _Active | None:
        with self._lock:
            active = self._active.get(worker_run_id)
            # A cancellation is accepted only while the matching run is still
            # active and before its terminal fence is set.  Without this
            # check a cancel racing just after a successful terminal RPC could
            # be reported as accepted even though the run had already
            # produced a successful artifact.  The context is attached before
            # the run is exposed to callers, so this decision is atomic with
            # respect to the dispatcher lock.
            if (active is None or active.binding_hash != binding_hash
                    or (active.context is not None and active.context.terminal)):
                return None
            active.cancelled.set()
            return active

    def finish(self, active: _Active) -> None:
        with self._lock:
            if self._active.get(active.worker_run_id) is active:
                self._active.pop(active.worker_run_id, None)


_DISPATCHER = _Dispatcher()


def _sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or any(
        item is None for item in (canonical_bytes, hash_jcs, verify_checkpoint, verify_provenance_receipt, verify_result_bundle, verify_attempt_result)
    ):
        raise ProjectionError("SDK_UNAVAILABLE", "public PlotPilot SDK unavailable")


def _identity() -> tuple[str, str]:
    try:
        value = load_runtime_identity()
        package_hash = value["package_hash"]
        release_id = value["release_id"]
        if not isinstance(package_hash, str) or not _HASH.fullmatch(package_hash) or package_hash == "0" * 64:
            raise ValueError("package_hash is invalid")
        if not isinstance(release_id, str) or not _HASH.fullmatch(release_id) or release_id == "0" * 64:
            raise ValueError("release_id is invalid")
        return package_hash, release_id
    except Exception as exc:
        raise ProjectionError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc


def _id(value: Any, path: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ProjectionError("INVALID_INPUT", f"{path} must be an identifier")
    return value


def _hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ProjectionError("INVALID_INPUT", f"{path} must be lowercase SHA-256")
    return value


def _integer(value: Any, path: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProjectionError("INVALID_INPUT", f"{path} must be integer >= {minimum}")
    return value


def _canonical(value: Any) -> bytes:
    _sdk()
    assert canonical_bytes is not None
    try:
        return bytes(canonical_bytes(value))
    except Exception as exc:
        raise ProjectionError("CANONICALIZATION_ERROR", str(exc)) from exc


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_json(prefix: str, value: Any) -> str:
    _sdk()
    assert hash_jcs is not None
    return str(hash_jcs(prefix, value))


def _binding_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build the stable identity shared by run and schema-valid cancel.

    The source receipt fields are run-only evidence.  A cancel control message
    may omit them, but all job/step/attempt, lease, snapshot and source
    binding fields remain part of the hash and therefore must match exactly.
    """
    binding = {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in _SOURCE_RECEIPT_FIELDS and key != "operation"
    }
    binding["operation"] = "run"
    return binding


def _closed(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        missing = sorted(fields - set(value)) if isinstance(value, Mapping) else sorted(fields)
        extra = sorted(set(value) - fields) if isinstance(value, Mapping) else []
        raise ProjectionError("INVALID_SOURCE", f"{label} is not closed (missing={missing}, extra={extra})")
    return dict(value)


def _array(value: Any, label: str, *, minimum: int = 0, maximum: int = 4096) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ProjectionError("INVALID_SOURCE", f"{label} must be an array in [{minimum}, {maximum}]")
    return value


def _rpc(host: HostPort | None, method: str, params: Mapping[str, object]) -> dict[str, object]:
    if host is None or not hasattr(host, "call"):
        raise ProjectionError("HOST_REQUIRED", "HostPort.call is required")
    try:
        response = host.call(method, dict(params))
    except ProjectionError:
        raise
    except Exception as exc:
        raise ProjectionError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}", retryable=True) from exc
    if not isinstance(response, Mapping):
        raise ProjectionError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    return dict(response)


def _read_asset(host: HostPort, asset_id: str, expected_hash: str) -> bytes:
    _id(asset_id, "asset_id")
    _hash(expected_hash, "asset_hash")
    chunks: list[bytes] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        response = _rpc(host, "host.asset.read/v1", {"asset_id": asset_id, "offset": offset, "length": _PAGE_SIZE})
        if set(response) != {"base64_chunk", "next_offset", "content_hash"}:
            raise ProjectionError("ASSET_READ_ERROR", "Host asset.read response is not closed")
        try:
            chunk = base64.b64decode(response["base64_chunk"], validate=True)
        except Exception as exc:
            raise ProjectionError("ASSET_READ_ERROR", "asset chunk is not valid base64") from exc
        if response["content_hash"] != _hash_bytes(chunk):
            raise ProjectionError("ASSET_READ_ERROR", "asset chunk hash mismatch")
        chunks.append(chunk)
        next_offset = response["next_offset"]
        if next_offset is None:
            break
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset != offset + len(chunk) or next_offset <= offset:
            raise ProjectionError("ASSET_READ_ERROR", "asset page offset is not contiguous")
        offset = next_offset
    else:
        raise ProjectionError("ASSET_READ_ERROR", "asset page limit exceeded")
    raw = b"".join(chunks)
    if _hash_bytes(raw) != expected_hash:
        raise ProjectionError("ASSET_HASH_MISMATCH", "source Asset hash differs")
    return raw


def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json(host: HostPort, asset_id: str, expected_hash: str) -> dict[str, Any]:
    raw = _read_asset(host, asset_id, expected_hash)
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicate, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except Exception as exc:
        raise ProjectionError("INVALID_SOURCE", "source Asset is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProjectionError("INVALID_SOURCE", "source Asset must contain an object")
    return value


def _narrative_parent_identity() -> dict[str, str]:
    """Load the immutable Narrative parent identity bundled with this wheel."""
    path = Path(__file__).resolve().with_name("narrative_parent_identity.json")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicate, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except Exception as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative parent identity sidecar is invalid") from exc
    fields = {"schema", "plugin_id", "capability_id", "package_hash", "release_id"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema") != "narrative-parent-identity/v1":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative parent identity sidecar is not closed")
    if value.get("plugin_id") != "com.plotpilot.novelagent.narrative-analysis" or value.get("capability_id") != "analysis.narrative.synthesize/v1":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative parent identity owner/capability differs")
    _hash(value.get("package_hash"), "narrative package_hash")
    _hash(value.get("release_id"), "narrative release_id")
    return {key: str(value[key]) for key in fields if key != "schema"}


def _upload(host: HostPort, raw: bytes, *, operation_key: str, suffix: str, mime: str = "application/json") -> tuple[str, str]:
    digest = _hash_bytes(raw)
    upload_id = "outline-upload:" + _hash_bytes(f"{operation_key}\n{suffix}\n{digest}".encode())[:48]
    chunks = [b""] if not raw else [raw[index:index + _PAGE_SIZE] for index in range(0, len(raw), _PAGE_SIZE)]
    accepted = 0
    asset_id: str | None = None
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _rpc(host, "host.asset.create/v1", {"operation_key": f"{operation_key}:{suffix}", "upload_id": upload_id, "offset": accepted, "mime": mime, "total_size": len(raw), "expected_hash": digest, "chunk_hash": _hash_bytes(chunk), "base64_chunk": base64.b64encode(chunk).decode("ascii"), "final": final})
        if response.get("upload_id") != upload_id or response.get("accepted_bytes") != accepted + len(chunk) or response.get("completed") is not final:
            raise ProjectionError("ASSET_UPLOAD_ERROR", "Host asset.create acknowledgement mismatch")
        returned_id = response.get("asset_id")
        if final:
            asset_id = _id(returned_id, "asset_id")
        elif returned_id is not None:
            raise ProjectionError("ASSET_UPLOAD_ERROR", "non-final upload returned an Asset ID")
        accepted += len(chunk)
    status = _rpc(host, "host.asset.upload.status/v1", {"upload_id": upload_id, "expected_hash": digest})
    if status.get("accepted_bytes") != len(raw) or status.get("completed") is not True or status.get("asset_id") != asset_id:
        raise ProjectionError("ASSET_UPLOAD_ERROR", "Host upload status acknowledgement mismatch")
    assert asset_id is not None
    return asset_id, digest


def _validate_span(raw: Any, label: str, *, canonical_text: str | None = None) -> dict[str, Any]:
    value = _closed(raw, {"evidence_span_id", "schema", "workspace_id", "document_id", "revision_id", "node_id", "start_codepoint", "end_codepoint", "quote", "quote_hash", "canonical_text_hash"}, label)
    if value["schema"] != "evidence-span/v1":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label}.schema is invalid")
    for field in ("evidence_span_id", "workspace_id", "document_id", "revision_id", "node_id"):
        _id(value[field], f"{label}.{field}")
    start = _integer(value["start_codepoint"], f"{label}.start_codepoint")
    end = _integer(value["end_codepoint"], f"{label}.end_codepoint", 1)
    quote = value["quote"]
    if not isinstance(quote, str) or end <= start or end - start != len(quote):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label} scalar bounds/quote mismatch")
    _hash(value["quote_hash"], f"{label}.quote_hash")
    _hash(value["canonical_text_hash"], f"{label}.canonical_text_hash")
    if _hash_bytes(quote.encode("utf-8")) != value["quote_hash"]:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label}.quote_hash does not match quote")
    if canonical_text is not None:
        if end > len(canonical_text) or canonical_text[start:end] != quote or _hash_bytes(canonical_text.encode("utf-8")) != value["canonical_text_hash"]:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label} is not contained by canonical Revision")
    return deepcopy(value)


def _validate_attribution(raw: Any, span_ids: set[str], label: str) -> dict[str, Any]:
    value = _closed(raw, {"attribution_id", "source_type", "workspace_id", "source_id", "revision_or_hash", "evidence_span_ids", "child_receipt_id"}, label)
    _id(value["attribution_id"], f"{label}.attribution_id")
    if not isinstance(value["source_type"], str) or not value["source_type"]:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label}.source_type is invalid")
    if value["workspace_id"] is not None:
        _id(value["workspace_id"], f"{label}.workspace_id")
    _id(value["source_id"], f"{label}.source_id")
    if not isinstance(value["revision_or_hash"], str) or not value["revision_or_hash"]:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label}.revision_or_hash is invalid")
    ids = _array(value["evidence_span_ids"], f"{label}.evidence_span_ids", minimum=1)
    if len(ids) != len(set(ids)) or any(_id(item, f"{label}.evidence_span_ids") not in span_ids for item in ids):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label} references an unknown EvidenceSpan")
    if value["child_receipt_id"] is not None:
        _id(value["child_receipt_id"], f"{label}.child_receipt_id")
    return deepcopy(value)


def _validate_unit(raw: Any, *, label: str, canonical_text: str | None = None) -> dict[str, Any]:
    value = _closed(raw, {"schema", "unit_id", "unit_kind", "title", "summary", "order", "evidence_spans", "source_attributions", "relations", "provenance"}, label)
    if value["schema"] != "narrative-unit/v1":
        raise ProjectionError("INVALID_SOURCE", f"{label}.schema is invalid")
    _id(value["unit_id"], f"{label}.unit_id")
    if not isinstance(value["unit_kind"], str) or not value["unit_kind"]:
        raise ProjectionError("INVALID_SOURCE", f"{label}.unit_kind is invalid")
    if not isinstance(value["title"], str) or not value["title"] or not isinstance(value["summary"], str):
        raise ProjectionError("INVALID_SOURCE", f"{label}.title/summary is invalid")
    _integer(value["order"], f"{label}.order")
    spans = _array(value["evidence_spans"], f"{label}.evidence_spans", minimum=1)
    normalized_spans = [_validate_span(item, f"{label}.evidence_spans[{i}]", canonical_text=canonical_text) for i, item in enumerate(spans)]
    span_ids = [item["evidence_span_id"] for item in normalized_spans]
    if len(span_ids) != len(set(span_ids)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label} contains duplicate EvidenceSpan IDs")
    attrs = _array(value["source_attributions"], f"{label}.source_attributions", minimum=1)
    normalized_attrs = [_validate_attribution(item, set(span_ids), f"{label}.source_attributions[{i}]") for i, item in enumerate(attrs)]
    attr_ids = [item["attribution_id"] for item in normalized_attrs]
    if len(attr_ids) != len(set(attr_ids)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label} contains duplicate attribution IDs")
    relations = _array(value["relations"], f"{label}.relations")
    normalized_relations: list[dict[str, str]] = []
    for index, relation in enumerate(relations):
        item = _closed(relation, {"relation_type", "target_unit_id"}, f"{label}.relations[{index}]")
        if not isinstance(item["relation_type"], str) or not item["relation_type"]:
            raise ProjectionError("INVALID_SOURCE", "relation_type is invalid")
        normalized_relations.append({"relation_type": item["relation_type"], "target_unit_id": _id(item["target_unit_id"], "target_unit_id")})
    provenance = _closed(value["provenance"], {"source_mode", "model_receipt_id"}, f"{label}.provenance")
    if provenance["source_mode"] not in {"model", "manual", "broker"} or (provenance["model_receipt_id"] is not None and not isinstance(provenance["model_receipt_id"], str)) or (provenance["source_mode"] == "model" and provenance["model_receipt_id"] is None):
        raise ProjectionError("INVALID_SOURCE", f"{label}.provenance is invalid")
    if provenance["model_receipt_id"] is not None:
        _id(provenance["model_receipt_id"], f"{label}.provenance.model_receipt_id")
    return {**deepcopy(value), "evidence_spans": normalized_spans, "source_attributions": normalized_attrs, "relations": normalized_relations}


def _validate_plan(raw: Any, expected_plan_id: str) -> dict[str, Any]:
    value = _closed(raw, {"schema", "plan_id", "version", "template_binding", "unit_refs", "hierarchy", "created_from_snapshot_hash", "immutable"}, "narrative_plan")
    if value["schema"] != "narrative-plan/v1" or value["plan_id"] != expected_plan_id or value["immutable"] is not True:
        raise ProjectionError("INVALID_SOURCE", "business narrative Plan identity/immutability is invalid")
    _id(value["plan_id"], "plan.plan_id")
    _integer(value["version"], "plan.version", 1)
    _hash(value["created_from_snapshot_hash"], "plan.created_from_snapshot_hash")
    binding = _closed(value["template_binding"], {"data_plugin_id", "data_release_id", "package_hash", "format_id", "template_id"}, "plan.template_binding")
    if binding["data_plugin_id"] != _DATA_PLUGIN_ID or binding["format_id"] != "plot-structure-template/v1":
        raise ProjectionError("INVALID_SOURCE", "plan template binding owner/format is invalid")
    _hash(binding["data_release_id"], "plan.data_release_id")
    _hash(binding["package_hash"], "plan.package_hash")
    _id(binding["template_id"], "plan.template_id")
    data_identity_path = Path(__file__).resolve().parents[3] / "data" / "plot-structure" / "v1" / "identity.json"
    if data_identity_path.is_file():
        try:
            data_identity = json.loads(data_identity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plot template Data identity is invalid") from exc
        if (binding["data_plugin_id"] != data_identity.get("plugin_id")
                or binding["data_release_id"] != data_identity.get("release_id")
                or binding["package_hash"] != data_identity.get("package_hash")):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan template Data identity differs")
    refs = _array(value["unit_refs"], "plan.unit_refs", minimum=1)
    normalized_refs: list[dict[str, Any]] = []
    ids: list[str] = []
    orders: list[int] = []
    for index, raw_ref in enumerate(refs):
        ref = _closed(raw_ref, {"unit_id", "payload_asset_id", "payload_hash", "order", "evidence_span_ids", "source_attribution_ids"}, f"plan.unit_refs[{index}]")
        unit_id = _id(ref["unit_id"], "plan.unit_id")
        asset_id = _id(ref["payload_asset_id"], "plan.payload_asset_id")
        payload_hash = _hash(ref["payload_hash"], "plan.payload_hash")
        order = _integer(ref["order"], "plan.unit.order")
        spans = _array(ref["evidence_span_ids"], "plan.unit.evidence_span_ids", minimum=1)
        attrs = _array(ref["source_attribution_ids"], "plan.unit.source_attribution_ids", minimum=1)
        if len(spans) != len(set(spans)) or any(not isinstance(item, str) for item in spans) or len(attrs) != len(set(attrs)) or any(not isinstance(item, str) for item in attrs):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan Unit evidence/attribution identities are invalid")
        normalized_refs.append({"unit_id": unit_id, "payload_asset_id": asset_id, "payload_hash": payload_hash, "order": order, "evidence_span_ids": list(spans), "source_attribution_ids": list(attrs)})
        ids.append(unit_id)
        orders.append(order)
    if len(ids) != len(set(ids)) or len(orders) != len(set(orders)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan Unit identity/order is not unique")
    hierarchy = _array(value["hierarchy"], "plan.hierarchy", minimum=1)
    normalized_hierarchy: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    order_ids: set[int] = set()
    for index, raw_node in enumerate(hierarchy):
        node = _closed(raw_node, {"node_id", "parent_node_id", "level", "title", "unit_ids", "order"}, f"plan.hierarchy[{index}]")
        node_id = _id(node["node_id"], "plan.node_id")
        if node_id in node_ids or node["level"] not in _LEVEL_INDEX:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy node identity/level is invalid")
        if node["parent_node_id"] == node_id:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy self-parent is invalid")
        parent = None if node["parent_node_id"] is None else _id(node["parent_node_id"], "plan.parent_node_id")
        title = node["title"]
        if not isinstance(title, str) or not title:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy title is invalid")
        order = _integer(node["order"], "plan.hierarchy.order")
        if order in order_ids:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy order is not unique")
        unit_ids = _array(node["unit_ids"], "plan.hierarchy.unit_ids")
        if len(unit_ids) != len(set(unit_ids)) or any(_id(item, "plan.hierarchy.unit_id") not in set(ids) for item in unit_ids):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy Unit coverage is invalid or missing")
        node_ids.add(node_id)
        order_ids.add(order)
        normalized_hierarchy.append({"node_id": node_id, "parent_node_id": parent, "level": node["level"], "title": title, "unit_ids": list(unit_ids), "order": order})
    roots = [item for item in normalized_hierarchy if item["parent_node_id"] is None]
    if len(roots) != 1 or roots[0]["level"] != "book":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy requires one book root")
    by_node = {item["node_id"]: item for item in normalized_hierarchy}
    for item in normalized_hierarchy:
        parent = item["parent_node_id"]
        if parent is not None:
            if parent not in by_node or _LEVEL_INDEX[by_node[parent]["level"]] >= _LEVEL_INDEX[item["level"]]:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy parent level/order is invalid")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy cycle detected")
        if node_id in visited:
            return
        visiting.add(node_id)
        parent = by_node[node_id]["parent_node_id"]
        if parent is not None:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in by_node:
        visit(node_id)
    coverage = [unit_id for node in normalized_hierarchy for unit_id in node["unit_ids"]]
    if set(coverage) != set(ids) or len(coverage) != len(set(coverage)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy must cover every Unit exactly once")
    return {**deepcopy(value), "template_binding": dict(binding), "unit_refs": normalized_refs, "hierarchy": normalized_hierarchy}


def _validate_child(raw: Any, label: str) -> dict[str, Any]:
    fields = {"binding_id", "child_job_id", "child_run_snapshot_hash", "result_contract", "result_bundle_asset_id", "result_bundle_hash", "provenance_receipt_id", "broker_invocation_asset_id", "broker_invocation_hash"}
    child = _closed(raw, fields, label)
    for field in ("binding_id", "child_job_id", "result_bundle_asset_id", "provenance_receipt_id", "broker_invocation_asset_id"):
        _id(child[field], f"{label}.{field}")
    for field in ("child_run_snapshot_hash", "result_bundle_hash", "broker_invocation_hash"):
        _hash(child[field], f"{label}.{field}")
    if child["result_contract"] not in _CHILD_CONTRACTS:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"{label}.result_contract is invalid")
    return deepcopy(child)


def _validate_synthesis(raw: Any, *, canonical_text: str | None = None) -> dict[str, Any]:
    value = _closed(raw, {"schema", "synthesis_id", "title", "summary", "plan_ref", "sections", "evidence_spans", "source_attributions", "broker_children"}, "narrative_synthesis")
    if value["schema"] != "narrative-synthesis/v1":
        raise ProjectionError("INVALID_SOURCE", "narrative Synthesis schema is invalid")
    _id(value["synthesis_id"], "synthesis.synthesis_id")
    if not isinstance(value["title"], str) or not value["title"] or not isinstance(value["summary"], str):
        raise ProjectionError("INVALID_SOURCE", "synthesis title/summary is invalid")
    plan_ref = _closed(value["plan_ref"], {"plan_id", "payload_asset_id", "payload_hash"}, "synthesis.plan_ref")
    _id(plan_ref["plan_id"], "synthesis.plan_ref.plan_id")
    _id(plan_ref["payload_asset_id"], "synthesis.plan_ref.payload_asset_id")
    _hash(plan_ref["payload_hash"], "synthesis.plan_ref.payload_hash")
    spans = _array(value["evidence_spans"], "synthesis.evidence_spans", minimum=1)
    normalized_spans = [_validate_span(item, f"synthesis.evidence_spans[{i}]", canonical_text=canonical_text) for i, item in enumerate(spans)]
    span_ids = [item["evidence_span_id"] for item in normalized_spans]
    if len(span_ids) != len(set(span_ids)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis EvidenceSpan IDs are not unique")
    attrs = _array(value["source_attributions"], "synthesis.source_attributions", minimum=1)
    normalized_attrs = [_validate_attribution(item, set(span_ids), f"synthesis.source_attributions[{i}]") for i, item in enumerate(attrs)]
    attr_ids = [item["attribution_id"] for item in normalized_attrs]
    if len(attr_ids) != len(set(attr_ids)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis attribution IDs are not unique")
    children = _array(value["broker_children"], "synthesis.broker_children", minimum=1)
    normalized_children = [_validate_child(item, f"synthesis.broker_children[{i}]") for i, item in enumerate(children)]
    child_ids = [item["provenance_receipt_id"] for item in normalized_children]
    if len(child_ids) != len(set(child_ids)):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Broker child receipt IDs are not unique")
    for attr in normalized_attrs:
        if attr["child_receipt_id"] is not None and attr["child_receipt_id"] not in set(child_ids):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "attribution references an unknown Broker child")
    sections = _array(value["sections"], "synthesis.sections", minimum=1)
    section_ids: set[str] = set()
    normalized_sections: list[dict[str, Any]] = []
    for index, raw_section in enumerate(sections):
        section = _closed(raw_section, {"section_id", "title", "summary", "unit_ids", "evidence_span_ids", "source_attribution_ids"}, f"synthesis.sections[{index}]")
        section_id = _id(section["section_id"], "section.section_id")
        if section_id in section_ids:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis section IDs are not unique")
        section_ids.add(section_id)
        unit_ids = _array(section["unit_ids"], "section.unit_ids", minimum=1)
        if len(unit_ids) != len(set(unit_ids)):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "section Unit IDs are not unique")
        unit_ids = [_id(item, "section.unit_id") for item in unit_ids]
        span_refs = _array(section["evidence_span_ids"], "section.evidence_span_ids", minimum=1)
        attr_refs = _array(section["source_attribution_ids"], "section.source_attribution_ids", minimum=1)
        if len(span_refs) != len(set(span_refs)) or any(_id(item, "section.evidence_span_id") not in set(span_ids) for item in span_refs) or len(attr_refs) != len(set(attr_refs)) or any(_id(item, "section.attribution_id") not in set(attr_ids) for item in attr_refs):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "section source references are invalid")
        if not isinstance(section["title"], str) or not section["title"] or not isinstance(section["summary"], str):
            raise ProjectionError("INVALID_SOURCE", "section title/summary is invalid")
        normalized_sections.append({"section_id": section_id, "title": section["title"], "summary": section["summary"], "unit_ids": unit_ids, "evidence_span_ids": list(span_refs), "source_attribution_ids": list(attr_refs)})
    return {**deepcopy(value), "plan_ref": dict(plan_ref), "sections": normalized_sections, "evidence_spans": normalized_spans, "source_attributions": normalized_attrs, "broker_children": normalized_children}


def _source_refs(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"workspace_id": request["workspace_id"], "source_type": "asset", "source_id": request["source_bundle_asset_id"], "revision_or_hash": request["source_bundle_hash"]}]


def _verify_source_receipt(
    host: HostPort,
    context: _Context,
    result_bundle: Mapping[str, Any],
    source_bundle_hash: str,
) -> _NarrativeParentClosure:
    """Verify the complete Narrative synthesis parent chain.

    The input Asset is required to be the public Narrative Result Bundle.  A
    raw ``narrative-synthesis/v1`` payload is never a valid parent: the
    receipt must bind the Result Bundle's ID/JCS hash and its producer, and the
    Bundle's sole synthesis Candidate must in turn bind the exact payload
    Asset/hash that Outline decodes.
    """
    request = context.request
    if not isinstance(result_bundle, Mapping):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative parent is not an object")
    # Reject payload masquerading before any receipt is trusted.  The public
    # verifier additionally checks the complete candidate-batch/item profile.
    if (result_bundle.get("schema") != "result-bundle/v1"
            or result_bundle.get("contract_id") != "candidate-batch/v1"
            or result_bundle.get("bundle_type") != "candidate_batch"):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative parent must be a candidate Result Bundle")
    _sdk()
    assert verify_result_bundle is not None
    try:
        verify_result_bundle(
            result_bundle,
            snapshot_workspace_id=request["workspace_id"],
            snapshot_hash_value=request["run_snapshot_hash"],
        )
    except Exception as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"Narrative Result Bundle is invalid: {exc}") from exc
    try:
        producer = _closed(
            result_bundle.get("producer"),
            {"plugin_id", "release_id", "capability_id", "job_id", "step_id", "attempt_id", "lease_epoch"},
            "narrative_result_bundle.producer",
        )
    except ProjectionError as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", str(exc)) from exc
    parent_identity = _narrative_parent_identity()
    if (producer["plugin_id"] != parent_identity["plugin_id"]
            or producer["release_id"] != parent_identity["release_id"]
            or producer["capability_id"] != parent_identity["capability_id"]):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative Result Bundle producer identity differs")
    if result_bundle.get("input_snapshot_hash") != request["run_snapshot_hash"]:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative Result Bundle snapshot differs")
    receipt_fields = _SOURCE_RECEIPT_FIELDS
    if not receipt_fields.issubset(request):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "immediate synthesis provenance receipt is required")
    receipt = _read_json(host, request["source_receipt_asset_id"], request["source_receipt_asset_hash"])
    _sdk()
    assert verify_provenance_receipt is not None
    try:
        verify_provenance_receipt(receipt)
    except Exception as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", f"source provenance receipt is invalid: {exc}") from exc
    expected_bundle_hash = _hash_json("result-bundle/v1", result_bundle)
    expected_receipt = {
        "receipt_id": request["source_receipt_id"],
        "plugin_id": parent_identity["plugin_id"],
        "release_id": parent_identity["release_id"],
        "package_hash": parent_identity["package_hash"],
        "capability_id": parent_identity["capability_id"],
        "job_id": producer["job_id"],
        "step_id": producer["step_id"],
        "attempt_id": producer["attempt_id"],
        "lease_epoch": producer["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "bundle_id": result_bundle["bundle_id"],
        "bundle_hash": expected_bundle_hash,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", f"source receipt {field} is not bound to the Narrative Result Bundle")
    if result_bundle.get("provenance_receipt_id") != receipt["receipt_id"]:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative Result Bundle receipt ID differs")
    items = result_bundle.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative synthesis Result Bundle must contain one Candidate")
    item = items[0]
    try:
        candidate = _closed(
            item,
            {"schema", "item_id", "item_kind", "target", "mutation", "payload_asset_id", "base", "write_set", "parent_candidate_ids", "source_refs", "status"},
            "narrative_result_bundle.item",
        )
        mutation = _closed(candidate.get("mutation"), {"mode", "payload_schema", "payload_hash"}, "narrative_result_bundle.item.mutation")
    except ProjectionError as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", str(exc)) from exc
    if candidate["schema"] != "candidate-item/v1" or mutation["mode"] != "relation_patch" or mutation["payload_schema"] != "narrative-synthesis/v1":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Narrative Result Bundle payload is not a synthesis Candidate")
    synthesis_asset_id = _id(candidate["payload_asset_id"], "synthesis payload_asset_id")
    synthesis_asset_hash = _hash(mutation["payload_hash"], "synthesis payload_hash")
    synthesis_raw = _read_asset(host, synthesis_asset_id, synthesis_asset_hash)
    try:
        synthesis = json.loads(
            synthesis_raw.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except Exception as exc:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis payload is not strict UTF-8 JSON") from exc
    if not isinstance(synthesis, dict) or synthesis.get("schema") != "narrative-synthesis/v1":
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "Candidate payload is not narrative-synthesis/v1")
    if _hash_bytes(synthesis_raw) != synthesis_asset_hash:
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis payload hash differs from Candidate binding")
    context.source_receipt = deepcopy(receipt)
    return _NarrativeParentClosure(
        receipt=deepcopy(receipt),
        result_bundle=dict(result_bundle),
        synthesis=synthesis,
        synthesis_asset_id=synthesis_asset_id,
        synthesis_asset_hash=synthesis_asset_hash,
    )


def _projection(
    synthesis: Mapping[str, Any],
    plan: Mapping[str, Any],
    units: Sequence[tuple[str, str, Mapping[str, Any]]],
    *,
    source_asset_id: str,
    source_asset_hash: str,
    views: Sequence[str],
    canonical_text: str | None = None,
    verify_asset_hash: bool = True,
    source_bundle_asset_id: str | None = None,
    source_bundle_hash: str | None = None,
) -> dict[str, Any]:
    clean_synthesis = _validate_synthesis(synthesis, canonical_text=canonical_text)
    clean_plan = _validate_plan(plan, clean_synthesis["plan_ref"]["plan_id"])
    expected_refs = {ref["unit_id"]: ref for ref in clean_plan["unit_refs"]}
    if len(units) != len(expected_refs):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "projection Unit set is incomplete")
    by_unit: dict[str, dict[str, Any]] = {}
    unit_identity: list[dict[str, str]] = []
    for asset_id, asset_hash, raw_unit in units:
        unit = _validate_unit(raw_unit, label="narrative_unit", canonical_text=canonical_text)
        unit_id = unit["unit_id"]
        if unit_id in by_unit:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "projection contains duplicate Unit identity")
        ref = expected_refs.get(unit_id)
        if ref is None or ref["payload_asset_id"] != asset_id or ref["payload_hash"] != asset_hash:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", f"Unit {unit_id} does not match plan Asset binding")
        if verify_asset_hash and _hash_bytes(_canonical(unit)) != asset_hash:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", f"Unit {unit_id} canonical hash differs")
        if [span["evidence_span_id"] for span in unit["evidence_spans"]] != ref["evidence_span_ids"] or [attr["attribution_id"] for attr in unit["source_attributions"]] != ref["source_attribution_ids"]:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", f"Unit {unit_id} source identities differ from plan")
        by_unit[unit_id] = unit
        unit_identity.append({"unit_id": unit_id, "asset_id": asset_id, "asset_hash": asset_hash})
    section_units = {item for section in clean_synthesis["sections"] for item in section["unit_ids"]}
    if section_units != set(by_unit) or sum(len(section["unit_ids"]) for section in clean_synthesis["sections"]) != len(by_unit):
        raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis sections do not cover the projection Unit set")
    synthesis_spans = {item["evidence_span_id"]: item for item in clean_synthesis["evidence_spans"]}
    synthesis_attrs = {item["attribution_id"]: item for item in clean_synthesis["source_attributions"]}
    span_ids = set(synthesis_spans)
    attr_ids = set(synthesis_attrs)
    for unit in by_unit.values():
        for span in unit["evidence_spans"]:
            span_id = span["evidence_span_id"]
            if span_id not in synthesis_spans or synthesis_spans[span_id] != span:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "Unit EvidenceSpan differs from synthesis closure")
        for attribution in unit["source_attributions"]:
            attribution_id = attribution["attribution_id"]
            if attribution_id not in synthesis_attrs or synthesis_attrs[attribution_id] != attribution:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "Unit attribution differs from synthesis closure")
        if not {item["evidence_span_id"] for item in unit["evidence_spans"]}.issubset(span_ids) or not {item["attribution_id"] for item in unit["source_attributions"]}.issubset(attr_ids):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "Unit source closure is absent from synthesis")
    # Each synthesis section must retain exactly the source identities of the
    # Units assigned to it.  Global membership alone would allow a projection
    # to move an otherwise valid span/attribution to another section.
    for section in clean_synthesis["sections"]:
        section_units = [by_unit[unit_id] for unit_id in section["unit_ids"]]
        expected_span_ids = {span["evidence_span_id"] for unit in section_units for span in unit["evidence_spans"]}
        expected_attr_ids = {attr["attribution_id"] for unit in section_units for attr in unit["source_attributions"]}
        if (set(section["evidence_span_ids"]) != expected_span_ids
                or set(section["source_attribution_ids"]) != expected_attr_ids):
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "synthesis section source references do not close over Units")
    requested = list(views)
    if not requested or len(requested) != len(set(requested)) or any(item not in _VIEWS for item in requested):
        raise ProjectionError("INVALID_INPUT", "views must be a unique non-empty subset of tree/card/timeline/relation")
    requested.sort(key=_VIEWS.index)
    hierarchy = sorted(clean_plan["hierarchy"], key=lambda item: (item["order"], item["node_id"]))
    tree_nodes: list[dict[str, Any]] = []
    for node in hierarchy:
        node_spans: set[str] = set()
        node_attrs: set[str] = set()
        for unit_id in node["unit_ids"]:
            if unit_id not in by_unit:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan hierarchy references an absent Unit")
            node_spans.update(item["evidence_span_id"] for item in by_unit[unit_id]["evidence_spans"])
            node_attrs.update(item["attribution_id"] for item in by_unit[unit_id]["source_attributions"])
        tree_nodes.append({"node_id": node["node_id"], "parent_node_id": node["parent_node_id"], "level": node["level"], "title": node["title"], "unit_ids": list(node["unit_ids"]), "order": node["order"], "evidence_span_ids": sorted(node_spans), "source_attribution_ids": sorted(node_attrs)})
    cards: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for unit in sorted(by_unit.values(), key=lambda item: (item["order"], item["unit_id"])):
        spans = sorted(item["evidence_span_id"] for item in unit["evidence_spans"])
        attrs = sorted(item["attribution_id"] for item in unit["source_attributions"])
        base = {"unit_id": unit["unit_id"], "unit_kind": unit["unit_kind"], "title": unit["title"], "summary": unit["summary"], "order": unit["order"], "evidence_span_ids": spans, "source_attribution_ids": attrs}
        cards.append(base)
        timeline.append({"event_id": "timeline-" + unit["unit_id"], "sequence": unit["order"], **base})
        for relation in unit["relations"]:
            target = relation["target_unit_id"]
            if target not in by_unit:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "relation target is absent")
            relations.append({"relation_id": "relation-" + _hash_bytes(f"{unit['unit_id']}:{relation['relation_type']}:{target}".encode())[:24], "source_unit_id": unit["unit_id"], "relation_type": relation["relation_type"], "target_unit_id": target, "evidence_span_ids": spans, "source_attribution_ids": attrs})
    relations.sort(key=lambda item: (item["source_unit_id"], item["relation_type"], item["target_unit_id"]))
    payloads = {"tree": {"schema": "outline-tree-projection/v1", "nodes": tree_nodes}, "card": {"schema": "outline-card-projection/v1", "cards": cards}, "timeline": {"schema": "outline-timeline-projection/v1", "events": timeline}, "relation": {"schema": "outline-relation-projection/v1", "relations": relations}}
    source = {"asset_id": source_asset_id, "asset_hash": source_asset_hash, "schema": "narrative-synthesis/v1", "synthesis_id": clean_synthesis["synthesis_id"], "plan_id": clean_plan["plan_id"]}
    closure = {"source": source, "plan": clean_plan, "units": sorted(unit_identity, key=lambda item: item["unit_id"]), "synthesis": {"synthesis_id": clean_synthesis["synthesis_id"], "sections": clean_synthesis["sections"], "evidence_spans": clean_synthesis["evidence_spans"], "source_attributions": clean_synthesis["source_attributions"], "broker_children": clean_synthesis["broker_children"]}, "views": requested}
    if source_bundle_asset_id is not None or source_bundle_hash is not None:
        if source_bundle_asset_id is None or source_bundle_hash is None:
            raise ProjectionError("SOURCE_CLOSURE_MISSING", "Result Bundle identity must be all-present")
        closure["result_bundle"] = {"asset_id": _id(source_bundle_asset_id, "source_bundle_asset_id"), "asset_hash": _hash(source_bundle_hash, "source_bundle_hash")}
    return {"schema": "outline-projection/v1", "projection_id": "projection-" + _hash_json("outline-projection-source/v1", closure)[:40], "source": source, "views": [{"mode": mode, "payload": payloads[mode]} for mode in requested], "evidence_spans": deepcopy(clean_synthesis["evidence_spans"]), "source_attributions": deepcopy(clean_synthesis["source_attributions"]), "broker_children": deepcopy(clean_synthesis["broker_children"]), "authority": {"mode": "read_only_projection", "authoritative_source": "narrative-synthesis/v1", "creates_second_authority": False, "free_form_canvas": False}}


def _context(request: Mapping[str, Any]) -> _Context:
    if not isinstance(request, Mapping):
        raise ProjectionError("INVALID_INPUT", "request must be an object")
    value = dict(request)
    allowed = {"schema", "capability_id", "operation_key", "operation", "job_id", "step_id", "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids", "provenance_receipt_id", "created_at", "total_units", "run_snapshot_hash", "workspace_id", "source_bundle_asset_id", "source_bundle_hash", "narrative_unit_assets", "views", "source_receipt_asset_id", "source_receipt_asset_hash", "source_receipt_id", "source_canonical_asset_id", "source_canonical_asset_hash"}
    if set(value) - allowed:
        raise ProjectionError("INVALID_INPUT", f"request fields are not closed: extra={sorted(set(value)-allowed)}")
    if value.get("schema") != INPUT_SCHEMA or value.get("capability_id") != CAPABILITY_ID:
        raise ProjectionError("INVALID_INPUT", "request schema/capability identity is invalid")
    operation = value.get("operation")
    if operation not in {"run", "cancel"}:
        raise ProjectionError("INVALID_INPUT", "operation is not declared")
    required = {"schema", "capability_id", "operation_key", "operation", "job_id", "step_id", "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids", "provenance_receipt_id", "created_at", "total_units", "run_snapshot_hash", "workspace_id", "source_bundle_asset_id", "source_bundle_hash", "narrative_unit_assets", "views"}
    # A run is only valid when its immediate Narrative synthesis parent is
    # explicitly identified.  Cancellation requests are control messages and
    # may omit the source receipt; the active run has already validated (or
    # will validate) its own parent before terminal fencing.
    if operation == "run":
        required |= {"source_receipt_id", "source_receipt_asset_id", "source_receipt_asset_hash"}
    if set(value) & {"source_receipt_asset_id", "source_receipt_asset_hash"} and not {"source_receipt_asset_id", "source_receipt_asset_hash", "source_receipt_id"}.issubset(value):
        raise ProjectionError("INVALID_INPUT", "source receipt binding must be all-present")
    if not required.issubset(value):
        raise ProjectionError("INVALID_INPUT", f"request fields are not closed: missing={sorted(required-set(value))}")
    if value["operation_key"] != CAPABILITY_ID:
        raise ProjectionError("INVALID_INPUT", "operation_key must equal capability_id")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id", "source_bundle_asset_id"):
        _id(value[field], field)
    _integer(value["lease_epoch"], "lease_epoch", 1)
    _integer(value["total_units"], "total_units", 1)
    _hash(value["run_snapshot_hash"], "run_snapshot_hash")
    _hash(value["source_bundle_hash"], "source_bundle_hash")
    if not isinstance(value["created_at"], str) or _TIME.fullmatch(value["created_at"]) is None:
        raise ProjectionError("INVALID_INPUT", "created_at is invalid")
    checkpoints = _array(value["checkpoint_ids"], "checkpoint_ids", minimum=1)
    if len(checkpoints) != len(set(checkpoints)):
        raise ProjectionError("INVALID_INPUT", "checkpoint_ids must be unique")
    [_id(item, "checkpoint_id") for item in checkpoints]
    refs = _array(value["narrative_unit_assets"], "narrative_unit_assets", minimum=1)
    for index, ref in enumerate(refs):
        item = _closed(ref, {"asset_id", "asset_hash"}, f"narrative_unit_assets[{index}]")
        _id(item["asset_id"], "unit asset_id")
        _hash(item["asset_hash"], "unit asset_hash")
    views = _array(value["views"], "views", minimum=1, maximum=4)
    if len(views) != len(set(views)) or any(item not in _VIEWS for item in views):
        raise ProjectionError("INVALID_INPUT", "views must be a unique subset of declared modes")
    if set(value) & _SOURCE_RECEIPT_FIELDS:
        if not _SOURCE_RECEIPT_FIELDS.issubset(value):
            raise ProjectionError("INVALID_INPUT", "source receipt binding must be all-present")
        _id(value["source_receipt_id"], "source_receipt_id")
        _id(value["source_receipt_asset_id"], "source_receipt_asset_id")
        _hash(value["source_receipt_asset_hash"], "source_receipt_asset_hash")
    if "source_canonical_asset_id" in value or "source_canonical_asset_hash" in value:
        if not {"source_canonical_asset_id", "source_canonical_asset_hash"}.issubset(value):
            raise ProjectionError("INVALID_INPUT", "canonical source binding must be all-present")
        _id(value["source_canonical_asset_id"], "source_canonical_asset_id")
        _hash(value["source_canonical_asset_hash"], "source_canonical_asset_hash")
    request_hash = _hash_json(INPUT_SCHEMA, value)
    binding = _binding_projection(value)
    binding_hash = _hash_json(CAPABILITY_ID + "-binding/v1", binding)
    return _Context(value, request_hash, binding_hash, tuple(checkpoints))


def _event(host: HostPort, context: _Context, event_type: str, payload_asset_id: str | None) -> None:
    context.last_local_seq += 1
    response = _rpc(host, "host.job.event/v1", {"operation_key": context.request_hash, "event_type": event_type, "payload_asset_id": payload_asset_id, "local_seq": context.last_local_seq})
    if response.get("accepted") is not True:
        raise ProjectionError("HOST_REJECTED", "Host rejected job event")
    seq = response.get("job_event_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= context.last_event_seq:
        raise ProjectionError("HOST_CONTRACT_ERROR", "job event sequence is not monotonic")
    context.last_event_seq = seq


def _checkpoint(host: HostPort, context: _Context, *, source_hash: str, projection_hash: str, completed_units: int) -> dict[str, Any]:
    if context.checkpoint_seq >= len(context.checkpoint_ids):
        raise ProjectionError("CHECKPOINT_ERROR", "checkpoint budget exhausted")
    package_hash, release_id = _identity()
    state = {"schema": "outline-projection-state/v1", "worker_run_id": context.request["worker_run_id"], "attempt_id": context.request["attempt_id"], "run_snapshot_hash": context.request["run_snapshot_hash"], "source_bundle_hash": source_hash, "projection_hash": projection_hash, "completed_units": completed_units, "total_units": context.request["total_units"], "package_hash": package_hash, "release_id": release_id}
    state_raw = _canonical(state)
    state_id, state_hash = _upload(host, state_raw, operation_key=context.request_hash, suffix="state")
    checkpoint = {"schema": "checkpoint/v1", "checkpoint_id": context.checkpoint_ids[context.checkpoint_seq], "checkpoint_seq": context.checkpoint_seq + 1, "job_id": context.request["job_id"], "step_id": context.request["step_id"], "source_attempt_id": context.request["attempt_id"], "lease_epoch": context.request["lease_epoch"], "run_snapshot_hash": context.request["run_snapshot_hash"], "replay_policy": "checkpoint_resume", "completed_units": completed_units, "total_units": context.request["total_units"], "unit_set_hash": state_hash, "state_asset_id": state_id, "created_at": context.request["created_at"]}
    checkpoint["checkpoint_hash"] = _hash_json("checkpoint/v1", checkpoint)
    _sdk()
    assert verify_checkpoint is not None
    try:
        verify_checkpoint(checkpoint, expected_snapshot_hash=context.request["run_snapshot_hash"], previous_seq=None)
    except Exception as exc:
        raise ProjectionError("CHECKPOINT_ERROR", str(exc)) from exc
    checkpoint_raw = _canonical(checkpoint)
    checkpoint_id, checkpoint_hash = _upload(host, checkpoint_raw, operation_key=context.request_hash, suffix="checkpoint")
    response = _rpc(host, "host.checkpoint.commit/v1", {"operation_key": context.request_hash + ":checkpoint", "checkpoint_asset_id": checkpoint_id})
    if response.get("accepted") is not True or response.get("checkpoint_id", checkpoint["checkpoint_id"]) != checkpoint["checkpoint_id"]:
        raise ProjectionError("CHECKPOINT_ERROR", "Host rejected checkpoint commit")
    context.checkpoint_seq += 1
    return {"checkpoint": checkpoint, "checkpoint_asset_id": checkpoint_id, "checkpoint_asset_hash": checkpoint_hash, "state_asset_id": state_id, "state_asset_hash": state_hash}


def _bundle(context: _Context, payload_asset_id: str, payload_hash: str) -> dict[str, Any]:
    _, release_id = _identity()
    request = context.request
    item_id = "outline-artifact:" + payload_hash[:48]
    bundle = {"schema": "result-bundle/v1", "contract_id": RESULT_CONTRACT, "bundle_id": "outline-bundle:" + _hash_json("outline-bundle/v1", {"request": context.request_hash, "payload_hash": payload_hash})[:48], "bundle_type": "artifact", "producer": {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": CAPABILITY_ID, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"]}, "input_snapshot_hash": request["run_snapshot_hash"], "items": [{"schema": "artifact-item/v1", "item_id": item_id, "artifact_kind": "outline.read-only-projection/v1", "payload_asset_id": payload_asset_id, "payload_hash": payload_hash, "mime": "application/json", "source_refs": _source_refs(request), "status": "complete"}], "warnings": [], "partial": False, "provenance_receipt_id": request["provenance_receipt_id"], "skill_chain_result_refs": []}
    _sdk()
    assert verify_result_bundle is not None and verify_attempt_result is not None
    try:
        verify_attempt_result(bundle, attempt_state="succeeded", snapshot_workspace_id=request["workspace_id"], snapshot_hash_value=request["run_snapshot_hash"])
    except Exception as exc:
        raise ProjectionError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return bundle


def _diagnostic(context: _Context, detail_asset_id: str, detail_hash: str, code: str, message: str) -> dict[str, Any]:
    _, release_id = _identity()
    request = context.request
    bundle = {"schema": "result-bundle/v1", "contract_id": "diagnostic-bundle/v1", "bundle_id": "outline-failure:" + detail_hash[:48], "bundle_type": "diagnostic", "producer": {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": CAPABILITY_ID, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"]}, "input_snapshot_hash": request["run_snapshot_hash"], "items": [{"schema": "diagnostic-item/v1", "item_id": "outline-error:" + detail_hash[:48], "severity": "error", "code": code, "message": message[:1024], "details_asset_id": detail_asset_id, "details_hash": detail_hash, "source_refs": [], "status": "failed"}], "warnings": [], "partial": True, "provenance_receipt_id": request["provenance_receipt_id"], "skill_chain_result_refs": []}
    _sdk()
    assert verify_result_bundle is not None and verify_attempt_result is not None
    try:
        verify_attempt_result(bundle, attempt_state="failed", snapshot_workspace_id=request["workspace_id"], snapshot_hash_value=request["run_snapshot_hash"])
    except Exception as exc:
        raise ProjectionError("RESULT_CONTRACT_ERROR", str(exc)) from exc
    return bundle


def _receipt(context: _Context, bundle: Mapping[str, Any] | None, parents: Sequence[str], staged: Sequence[str] = ()) -> dict[str, Any]:
    package_hash, release_id = _identity()
    request = context.request
    receipt = {"schema": "provenance-receipt/v1", "receipt_id": request["provenance_receipt_id"], "plugin_id": PLUGIN_ID, "release_id": release_id, "package_hash": package_hash, "capability_id": CAPABILITY_ID, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "bundle_id": None if bundle is None else bundle["bundle_id"], "bundle_hash": None if bundle is None else _hash_json("result-bundle/v1", bundle), "parent_receipt_ids": list(dict.fromkeys(str(item) for item in parents)), "model_receipt_ids": [], "skill_chain_result_refs": [], "staged_items": list(dict.fromkeys(str(item) for item in staged)), "created_at": request["created_at"]}
    receipt["receipt_hash"] = _hash_json("provenance-receipt/v1", receipt)
    _sdk()
    assert verify_provenance_receipt is not None
    try:
        verify_provenance_receipt(receipt)
    except Exception as exc:
        raise ProjectionError("RECEIPT_CONTRACT_ERROR", str(exc)) from exc
    return receipt


def _complete(host: HostPort, context: _Context, outcome: str, *, bundle_asset_id: str | None, detail_asset_id: str | None, active: _Active | None = None) -> None:
    # Treat cancellation observed before the terminal fence as authoritative.
    # Once this function sets ``context.terminal`` the operation is fenced and
    # an unknown/malformed ACK cannot cause a second terminal transition.
    if outcome == "succeeded" and active is not None and active.cancelled.is_set():
        outcome = "cancelled"
        bundle_asset_id = None
    if context.terminal:
        raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED", "a terminal outcome was already dispatched")
    # Set the fence before the RPC: a malformed/unknown ACK can never trigger a
    # second failed terminal from the catch-all path.
    context.terminal = True
    context.terminal_outcome = outcome
    request = context.request
    response = _rpc(host, "host.job.complete/v1", {"operation_key": context.request_hash + ":complete", "worker_run_id": request["worker_run_id"], "outcome": outcome, "result_bundle_asset_id": bundle_asset_id, "candidate_stage_operation_key": None, "terminal_detail_asset_id": detail_asset_id, "local_seq": context.last_local_seq + 1})
    if outcome not in {"succeeded", "failed", "cancelled"} or response.get("accepted") is not True or response.get("attempt_state") != outcome or response.get("step_state") != outcome or response.get("job_state") != outcome or response.get("provenance_receipt_id") != request["provenance_receipt_id"]:
        raise TerminalContractError("TERMINAL_ACK_INVALID", "Host terminal acknowledgement is not bound to this attempt")
    seq = response.get("job_event_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= context.last_event_seq:
        raise TerminalContractError("TERMINAL_ACK_INVALID", "Host terminal event sequence is not monotonic")
    context.last_event_seq = seq
    context.last_local_seq += 1


class OutlineProjectionPlugin:
    plugin_id = PLUGIN_ID
    capability_id = CAPABILITY_ID

    def __init__(self) -> None:
        self.last_receipt: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None

    def cancel(self, request: Mapping[str, Any]) -> dict[str, object]:
        context = _context(request)
        active = _DISPATCHER.cancel(context.request["worker_run_id"], context.binding_hash)
        return {"accepted": active is not None, "worker_run_id": context.request["worker_run_id"]}

    def _cancelled(self, host: HostPort, context: _Context, active: _Active, *, detail_asset_id: str | None = None) -> None:
        receipt = _receipt(context, None, self._parent_receipts(context))
        receipt_asset, _ = _upload(host, _canonical(receipt), operation_key=context.request_hash, suffix="cancel-receipt")
        self.last_receipt = receipt
        _complete(host, context, "cancelled", bundle_asset_id=None, detail_asset_id=detail_asset_id or receipt_asset)

    def _parent_receipts(self, context: _Context, synthesis: Mapping[str, Any] | None = None) -> list[str]:
        request = context.request
        result: list[str] = []
        if context.source_receipt is not None:
            result.append(str(context.source_receipt["receipt_id"]))
        if synthesis is not None:
            result.extend(str(item["provenance_receipt_id"]) for item in synthesis.get("broker_children", []) if isinstance(item, Mapping) and item.get("provenance_receipt_id"))
        return list(dict.fromkeys(result))

    def _failure(self, host: HostPort, context: _Context, active: _Active, error: BaseException) -> dict[str, Any] | None:
        if context.terminal:
            raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED", f"terminal {context.terminal_outcome!r} already dispatched") from error
        if active.cancelled.is_set():
            self._cancelled(host, context, active)
            return None
        code = getattr(error, "code", "INTERNAL_ERROR")
        message = str(error)[:1024]
        detail = {"schema": "outline-projection-error/v1", "code": code, "message": message, "retryable": bool(getattr(error, "retryable", False))}
        detail_asset, detail_hash = _upload(host, _canonical(detail), operation_key=context.request_hash, suffix="failure-detail")
        bundle = _diagnostic(context, detail_asset, detail_hash, code, message)
        bundle_asset, _ = _upload(host, _canonical(bundle), operation_key=context.request_hash, suffix="failure-bundle")
        receipt = _receipt(context, bundle, self._parent_receipts(context))
        receipt_asset, _ = _upload(host, _canonical(receipt), operation_key=context.request_hash, suffix="failure-receipt")
        self.last_receipt = receipt
        _event(host, context, "outline-projection.failed", detail_asset)
        _complete(host, context, "failed", bundle_asset_id=bundle_asset, detail_asset_id=receipt_asset)
        return bundle

    def run(self, request: Mapping[str, Any], host: HostPort | None = None) -> dict[str, Any] | None:
        context: _Context | None = None
        active: _Active | None = None
        try:
            context = _context(request)
            if context.request["operation"] == "cancel":
                return self.cancel(request)
            if host is None:
                raise ProjectionError("HOST_REQUIRED", "HostPort.call is required")
            active = _DISPATCHER.begin(context.request["worker_run_id"], context.binding_hash)
            active.context = context
            _event(host, context, "outline-projection.started", None)
            if active.cancelled.is_set():
                self._cancelled(host, context, active)
                return None
            result_bundle = _read_json(host, context.request["source_bundle_asset_id"], context.request["source_bundle_hash"])
            parent = _verify_source_receipt(host, context, result_bundle, context.request["source_bundle_hash"])
            synthesis = parent.synthesis
            canonical_text = None
            if "source_canonical_asset_id" in context.request:
                canonical_raw = _read_asset(host, context.request["source_canonical_asset_id"], context.request["source_canonical_asset_hash"])
                try:
                    canonical_text = canonical_raw.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise ProjectionError("INVALID_SOURCE", "canonical source Asset is not UTF-8") from exc
            synthesis = _validate_synthesis(synthesis, canonical_text=canonical_text)
            plan = _validate_plan(_read_json(host, synthesis["plan_ref"]["payload_asset_id"], synthesis["plan_ref"]["payload_hash"]), synthesis["plan_ref"]["plan_id"])
            if plan["created_from_snapshot_hash"] != context.request["run_snapshot_hash"]:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "plan snapshot differs from projection snapshot")
            loaded_units: list[tuple[str, str, Mapping[str, Any]]] = []
            for ref in context.request["narrative_unit_assets"]:
                loaded_units.append((ref["asset_id"], ref["asset_hash"], _read_json(host, ref["asset_id"], ref["asset_hash"])))
            projection = _projection(
                synthesis,
                plan,
                loaded_units,
                source_asset_id=parent.synthesis_asset_id,
                source_asset_hash=parent.synthesis_asset_hash,
                source_bundle_asset_id=context.request["source_bundle_asset_id"],
                source_bundle_hash=context.request["source_bundle_hash"],
                views=context.request["views"],
                canonical_text=canonical_text,
            )
            projection_raw = _canonical(projection)
            projection_hash = _hash_bytes(projection_raw)
            checkpoint = _checkpoint(host, context, source_hash=context.request["source_bundle_hash"], projection_hash=projection_hash, completed_units=len(loaded_units))
            self.last_checkpoint = checkpoint
            _event(host, context, "outline-projection.checkpoint", checkpoint["checkpoint_asset_id"])
            if active.cancelled.is_set():
                self._cancelled(host, context, active, detail_asset_id=checkpoint["checkpoint_asset_id"])
                return None
            payload_asset, payload_hash = _upload(host, projection_raw, operation_key=context.request_hash, suffix="projection")
            if active.cancelled.is_set():
                self._cancelled(host, context, active, detail_asset_id=checkpoint["checkpoint_asset_id"])
                return None
            bundle = _bundle(context, payload_asset, payload_hash)
            bundle_asset, _ = _upload(host, _canonical(bundle), operation_key=context.request_hash, suffix="result-bundle")
            if active.cancelled.is_set():
                self._cancelled(host, context, active, detail_asset_id=checkpoint["checkpoint_asset_id"])
                return None
            receipt = _receipt(context, bundle, self._parent_receipts(context, synthesis))
            receipt_asset, _ = _upload(host, _canonical(receipt), operation_key=context.request_hash, suffix="receipt")
            self.last_receipt = receipt
            if active.cancelled.is_set():
                self._cancelled(host, context, active, detail_asset_id=checkpoint["checkpoint_asset_id"])
                return None
            _event(host, context, "outline-projection.result", bundle_asset)
            if active.cancelled.is_set():
                self._cancelled(host, context, active, detail_asset_id=checkpoint["checkpoint_asset_id"])
                return None
            _complete(host, context, "succeeded", bundle_asset_id=bundle_asset, detail_asset_id=receipt_asset, active=active)
            return bundle
        except TerminalContractError:
            raise
        except Exception as exc:
            if context is None or active is None:
                raise
            return self._failure(host, context, active, exc)
        finally:
            if active is not None:
                _DISPATCHER.finish(active)


PACKAGE_HASH, RELEASE_ID = _identity()


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    selected = CAPABILITY_ID if capability_id is None else capability_id
    if selected != CAPABILITY_ID:
        raise KeyError(selected)
    _, release_id = _identity()
    return descriptor(release_id)


_RUNTIME = OutlineProjectionPlugin()

def render_projection(
    synthesis: Mapping[str, Any],
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any] | tuple[str, str, Mapping[str, Any]]],
    *,
    source_asset_id: str,
    source_asset_hash: str,
    views: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pure helper retained for callers that already decoded Narrative Assets.

    The Host runtime supplies the immutable ``(asset_id, asset_hash, value)``
    tuples.  A pure caller may provide decoded Unit objects; they are bound to
    the plan's declared Asset identity and their canonical bytes are hashed
    against that declaration before projection, so no unverifiable local
    binding can be invented.
    """
    refs = {ref["unit_id"]: ref for ref in plan.get("unit_refs", []) if isinstance(ref, Mapping) and "unit_id" in ref}
    packed: list[tuple[str, str, Mapping[str, Any]]] = []
    for item in units:
        if isinstance(item, tuple) and len(item) == 3:
            packed.append(item)
        elif isinstance(item, Mapping):
            unit_id = item.get("unit_id")
            if unit_id not in refs:
                raise ProjectionError("SOURCE_CLOSURE_MISSING", "pure Unit is absent from plan refs")
            packed.append((refs[unit_id]["payload_asset_id"], refs[unit_id]["payload_hash"], item))
        else:
            raise ProjectionError("INVALID_SOURCE", "pure Unit must be an object")
    return _projection(synthesis, plan, packed, source_asset_id=source_asset_id, source_asset_hash=source_asset_hash, views=list(_VIEWS) if views is None else views)


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> dict[str, Any] | None:
    if request is None:
        return capability_descriptor()
    if isinstance(request, Mapping) and request.get("operation") == "cancel":
        return _RUNTIME.cancel(request)
    return _RUNTIME.run(request, host)


__all__ = ["CAPABILITY_ID", "HostPort", "OUTPUT_SCHEMA", "PACKAGE_HASH", "PLUGIN_ID", "ProjectionError", "RELEASE_ID", "OutlineProjectionPlugin", "capability_descriptor", "main", "render_projection"]
