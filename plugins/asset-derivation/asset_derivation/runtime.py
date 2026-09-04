"""Host-bound Asset Derivation worker with strict target and package gates."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .capability_spec import CAPABILITIES, PLUGIN_ID, SPEC_BY_CAPABILITY, descriptors
from .contract import (
    AssetContractError,
    DataBundleError,
    TARGET_SCHEMA_FILES,
    TargetSchemaError,
    build_data_bundle,
    canonical_json_bytes,
    expected_interpreter_mappings,
    hash_json,
    independent_package_identity,
    validate_target_payload,
    verify_data_bundle_identity,
    verify_interpreter_mappings,
)

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as sdk_canonical_bytes
    from plotpilot_plugin_sdk.verifier import validate_rpc_result, verify_checkpoint, verify_result_bundle
except ImportError:  # pragma: no cover - a wheel without the SDK cannot perform a Host run
    sdk_canonical_bytes = None
    validate_rpc_result = verify_checkpoint = verify_result_bundle = None

try:
    from .package_identity import load_runtime_identity
except Exception:  # pragma: no cover - source inspection may omit generated identity
    load_runtime_identity = None


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TIME_RE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)

CAPABILITY_TEMPLATE_DERIVE = "asset.template.derive/v1"
CAPABILITY_DATA_PACKAGE = "asset.data-plugin.package/v1"

# NAP-04 owns exactly these immutable Data identities.  Keeping this table
# next to the runtime gate prevents a caller from presenting a foreign Data
# plugin with an otherwise well-formed package/bundle.
DATA_PLUGIN_BY_FORMAT = {
    "character-archetype/v1": "com.plotpilot.novelagent.character-archetype",
    "world-rule-template/v1": "com.plotpilot.novelagent.world-rule-template",
}


class AssetWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TerminalContractError(AssetWorkerError):
    """Raised when a Host acknowledges a durable effect with an invalid shape."""


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass
class _Context:
    request_hash: str
    binding_hash: str
    local_seq: int = 0
    stage_response: dict[str, Any] | None = None


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise AssetWorkerError("INPUT_INVALID", f"{label} must be identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise AssetWorkerError("INPUT_INVALID", f"{label} must be SHA-256")
    return value


def _int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AssetWorkerError("INPUT_INVALID", f"{label} must be integer >= {minimum}")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        if sdk_canonical_bytes is not None:
            return bytes(sdk_canonical_bytes(value))
        return canonical_json_bytes(value)
    except Exception as exc:  # pragma: no cover - canonical library owns the detail
        raise AssetWorkerError("CANONICALIZATION_ERROR", str(exc)) from exc


def _strict_json(raw: bytes, code: str = "ASSET_READ_ERROR") -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except Exception as exc:
        raise AssetWorkerError(code, "Asset is not strict UTF-8 JSON") from exc


def _host_call(host: HostPort | None, method: str, params: Mapping[str, object]) -> dict[str, Any]:
    if host is None or not hasattr(host, "call"):
        raise AssetWorkerError("HOST_REQUIRED", "Core HostPort.call required")
    try:
        response = host.call(method, dict(params))
    except AssetWorkerError:
        raise
    except Exception as exc:
        raise AssetWorkerError("HOST_RPC_ERROR", f"{method}: {exc}", True) from exc
    if not isinstance(response, Mapping):
        raise AssetWorkerError("HOST_CONTRACT_ERROR", f"{method} returned non-object")
    result = dict(response)
    if validate_rpc_result is not None:
        try:
            validate_rpc_result(method, result, request={"method": method, "params": dict(params)})
        except Exception as exc:
            raise AssetWorkerError("HOST_CONTRACT_ERROR", f"{method} result invalid: {exc}") from exc
    return result


def _read_asset(
    host: HostPort | None,
    asset_id: str,
    expected_hash: str,
    code: str = "ASSET_READ_ERROR",
) -> bytes:
    _id(asset_id, "asset_id")
    _hash(expected_hash, "asset_hash")
    if host is None:
        raise AssetWorkerError("HOST_REQUIRED", "asset read requires HostPort")
    offset = 0
    chunks: list[bytes] = []
    for _ in range(4096):
        response = _host_call(
            host,
            "host.asset.read/v1",
            {"asset_id": asset_id, "offset": offset, "length": 8_388_608},
        )
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise AssetWorkerError(code, "missing base64_chunk")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise AssetWorkerError(code, "invalid base64") from exc
        if hashlib.sha256(chunk).hexdigest() != response.get("content_hash"):
            raise AssetWorkerError(code, "page content hash mismatch")
        chunks.append(chunk)
        next_offset = response.get("next_offset")
        if next_offset is None:
            break
        if (
            isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or next_offset != offset + len(chunk)
            or next_offset <= offset
        ):
            raise AssetWorkerError(code, "non-contiguous pages")
        offset = next_offset
    else:
        raise AssetWorkerError(code, "Asset page limit exceeded")
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise AssetWorkerError(code, "Asset hash mismatch")
    return data


def _load_json_asset(
    host: HostPort | None, asset_id: str, asset_hash: str, code: str = "ASSET_READ_ERROR"
) -> Any:
    return _strict_json(_read_asset(host, asset_id, asset_hash, code), code)


def _upload(host: HostPort | None, context: _Context, data: bytes, mime: str, suffix: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    asset_id = "asset-" + digest[:48]
    if host is None:
        return asset_id
    upload_id = f"{context.request_hash}-upload-{suffix}"
    chunks = [data[i : i + 8_388_608] for i in range(0, len(data), 8_388_608)] or [b""]
    offset = 0
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        response = _host_call(
            host,
            "host.asset.create/v1",
            {
                "operation_key": context.request_hash,
                "upload_id": upload_id,
                "offset": offset,
                "mime": mime,
                "total_size": len(data),
                "expected_hash": digest,
                "chunk_hash": hashlib.sha256(chunk).hexdigest(),
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "final": final,
            },
        )
        if (
            response.get("upload_id") != upload_id
            or response.get("accepted_bytes") != offset + len(chunk)
            or response.get("completed") is not final
        ):
            raise AssetWorkerError("ASSET_CREATE_ERROR", "upload acknowledgement mismatch")
        if final:
            asset_id = _id(response.get("asset_id"), "final asset_id")
        offset += len(chunk)
    status = _host_call(
        host,
        "host.asset.upload.status/v1",
        {"upload_id": upload_id, "expected_hash": digest},
    )
    if (
        status.get("accepted_bytes") != len(data)
        or status.get("completed") is not True
        or status.get("asset_id") != asset_id
    ):
        raise AssetWorkerError("ASSET_UPLOAD_ERROR", "upload status mismatch")
    return asset_id


def _validate_request(request: Mapping[str, Any], capability: str) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise AssetWorkerError("INPUT_INVALID", "request must object")
    value = dict(request)
    spec = SPEC_BY_CAPABILITY[capability]
    common = {
        "schema", "capability_id", "operation_key", "operation", "job_id", "step_id",
        "attempt_id", "worker_run_id", "lease_epoch", "checkpoint_ids", "provenance_receipt_id",
        "created_at", "total_units", "run_snapshot_hash", "workspace_id",
    }
    groups = {
        CAPABILITY_TEMPLATE_DERIVE: {
            "source_asset_id", "source_asset_hash", "source_format_id", "target_format_id",
            "target_schema_id", "model_profile_revision_id", "source_projection",
        },
        CAPABILITY_DATA_PACKAGE: {
            "format_id", "data_plugin_id", "version", "root_path", "files", "package_hash",
            "release_id", "interpreter_mappings",
        },
    }
    operation = value.get("operation")
    if operation not in spec.supports:
        raise AssetWorkerError("INPUT_INVALID", f"operation {operation!r} not declared")
    allowed = common | groups[capability]
    if operation == "resume":
        allowed |= {
            "resume_checkpoint_asset_id", "resume_checkpoint_asset_hash",
            "resume_state_asset_id", "resume_state_asset_hash",
        }
    if set(value) != allowed:
        raise AssetWorkerError(
            "INPUT_INVALID",
            f"request fields are not closed: missing={sorted(allowed - set(value))}, "
            f"extra={sorted(set(value) - allowed)}",
        )
    if (
        value.get("schema") != spec.input_schema
        or value.get("capability_id") != capability
        or value.get("operation_key") != capability
    ):
        raise AssetWorkerError("INPUT_INVALID", "request schema/capability/operation_key mismatch")
    for field in (
        "job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id", "workspace_id"
    ):
        _id(value[field], field)
    _int(value["lease_epoch"], "lease_epoch", 1)
    _int(value["total_units"], "total_units", 1)
    _hash(value["run_snapshot_hash"], "run_snapshot_hash")
    if not isinstance(value["created_at"], str) or TIME_RE.fullmatch(value["created_at"]) is None:
        raise AssetWorkerError("INPUT_INVALID", "created_at invalid")
    checkpoint_ids = value["checkpoint_ids"]
    if not isinstance(checkpoint_ids, list) or not checkpoint_ids or len(set(checkpoint_ids)) != len(checkpoint_ids):
        raise AssetWorkerError("INPUT_INVALID", "checkpoint_ids invalid")
    for checkpoint_id in checkpoint_ids:
        _id(checkpoint_id, "checkpoint_id")

    if capability == CAPABILITY_TEMPLATE_DERIVE:
        for field in (
            "source_asset_id", "source_format_id", "target_format_id", "target_schema_id",
            "model_profile_revision_id",
        ):
            _id(value[field], field)
        _hash(value["source_asset_hash"], "source_asset_hash")
        if value["target_format_id"] not in TARGET_SCHEMA_FILES:
            raise AssetWorkerError("INPUT_INVALID", "target_format_id has no frozen closed schema")
        if value["target_schema_id"] != value["target_format_id"]:
            raise AssetWorkerError("INPUT_INVALID", "target_schema_id must equal target_format_id")
        if not isinstance(value["source_projection"], Mapping):
            raise AssetWorkerError("INPUT_INVALID", "source_projection must object")
        if set(value["source_projection"]) - {"source_format_id", "payload"}:
            raise AssetWorkerError("INPUT_INVALID", "source_projection contains undeclared fields")
        if "source_format_id" in value["source_projection"]:
            _id(value["source_projection"]["source_format_id"], "source_projection.source_format_id")
        if "payload" in value["source_projection"] and not isinstance(value["source_projection"]["payload"], Mapping):
            raise AssetWorkerError("INPUT_INVALID", "source_projection.payload must object")
    else:
        for field in ("format_id", "data_plugin_id", "version"):
            _id(value[field], field)
        expected_plugin = DATA_PLUGIN_BY_FORMAT.get(value["format_id"])
        if expected_plugin is None or value["data_plugin_id"] != expected_plugin:
            raise AssetWorkerError("INPUT_INVALID", "format_id/data_plugin_id is not an owned frozen Data identity")
        if value["version"] != "1.0.0":
            raise AssetWorkerError("INPUT_INVALID", "only frozen Data version 1.0.0 is accepted")
        if not isinstance(value["root_path"], str) or not value["root_path"]:
            raise AssetWorkerError("INPUT_INVALID", "root_path must be non-empty")
        _hash(value["package_hash"], "package_hash")
        _hash(value["release_id"], "release_id")
        files = value["files"]
        if not isinstance(files, list) or not files:
            raise AssetWorkerError("INPUT_INVALID", "files required")
        for index, row in enumerate(files):
            if not isinstance(row, Mapping) or set(row) != {"path", "asset_id", "sha256", "mime", "size"}:
                raise AssetWorkerError("INPUT_INVALID", f"files[{index}] closed fields invalid")
            if not isinstance(row["path"], str) or not row["path"]:
                raise AssetWorkerError("INPUT_INVALID", f"files[{index}].path invalid")
            _id(row["asset_id"], f"files[{index}].asset_id")
            _hash(row["sha256"], f"files[{index}].sha256")
            _int(row["size"], f"files[{index}].size")
            if not isinstance(row["mime"], str) or not row["mime"]:
                raise AssetWorkerError("INPUT_INVALID", f"files[{index}].mime invalid")
        verify_interpreter_mappings(value["interpreter_mappings"], format_id=value["format_id"])

    if operation == "resume":
        for field in ("resume_checkpoint_asset_id", "resume_state_asset_id"):
            _id(value[field], field)
        for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"):
            _hash(value[field], field)
    return value


def _context(value: Mapping[str, Any], capability: str) -> _Context:
    request_hash = hash_json(capability + "-request/v1", value)
    binding_input = {
        key: deepcopy(item)
        for key, item in value.items()
        if key != "operation" and not key.startswith("resume_")
    }
    return _Context(request_hash, hash_json(capability + "-binding/v1", binding_input))


def _event(
    value: Mapping[str, Any],
    context: _Context,
    host: HostPort | None,
    event_type: str,
    payload_asset_id: str | None = None,
) -> None:
    if host is None:
        return
    context.local_seq += 1
    response = _host_call(
        host,
        "host.job.event/v1",
        {
            "operation_key": value["operation_key"],
            "event_type": event_type,
            "payload_asset_id": payload_asset_id,
            "local_seq": context.local_seq,
        },
    )
    if response.get("accepted") is not True:
        raise AssetWorkerError("HOST_REJECTED", f"Core rejected job event {event_type}")


def _candidate_item(
    value: Mapping[str, Any], payload: bytes, payload_asset_id: str, payload_schema: str, item_id: str
) -> dict[str, Any]:
    payload_hash = hashlib.sha256(payload).hexdigest()
    revision_id = value.get("source_format_id", value["step_id"])
    content_hash = value.get("source_asset_hash", value["run_snapshot_hash"])
    return {
        "schema": "candidate-item/v1", "item_id": item_id, "item_kind": "relation_set",
        "target": {"workspace_id": value["workspace_id"], "entity_kind": "relation_set", "entity_id": item_id},
        "mutation": {"mode": "relation_patch", "payload_schema": payload_schema, "payload_hash": payload_hash},
        "payload_asset_id": payload_asset_id,
        "base": {"revision_id": revision_id, "content_hash": content_hash},
        "write_set": [{"workspace_id": value["workspace_id"], "entity_kind": "relation_set", "entity_id": item_id, "revision_id": revision_id, "content_hash": content_hash}],
        "parent_candidate_ids": [],
        "source_refs": [{"workspace_id": value["workspace_id"], "source_type": "asset", "source_id": value.get("source_asset_id", value["job_id"]), "revision_or_hash": content_hash}],
        "status": "complete",
    }


def _artifact_item(value: Mapping[str, Any], payload_asset_id: str, payload_hash: str, item_id: str) -> dict[str, Any]:
    return {
        "schema": "artifact-item/v1", "item_id": item_id, "artifact_kind": "plugin-data-bundle",
        "payload_asset_id": payload_asset_id, "payload_hash": payload_hash, "mime": "application/json",
        "source_refs": [{"workspace_id": value["workspace_id"], "source_type": "data-package-input", "source_id": value["data_plugin_id"], "revision_or_hash": value["package_hash"]}],
        "status": "complete",
    }


def _bundle(
    value: Mapping[str, Any], capability: str, items: list[dict[str, Any]], release_id: str,
    contract_id: str = "candidate-batch/v1", partial: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "result-bundle/v1", "contract_id": contract_id,
        "bundle_id": f"{capability.replace('/', '-')}-{value['attempt_id']}-bundle",
        # Keep the public Result Bundle profile in lockstep with its
        # contract.  Diagnostics are neither artifacts nor candidates; a
        # failed attempt must be represented as diagnostic-bundle/v1 with the
        # corresponding ``diagnostic`` bundle type.
        "bundle_type": (
            "candidate_batch"
            if contract_id == "candidate-batch/v1"
            else "artifact"
            if contract_id == "artifact-bundle/v1"
            else "diagnostic"
        ),
        "producer": {"plugin_id": PLUGIN_ID, "release_id": release_id, "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"], "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"]},
        "input_snapshot_hash": value["run_snapshot_hash"], "items": items, "warnings": [], "partial": partial,
        "provenance_receipt_id": value["provenance_receipt_id"], "skill_chain_result_refs": [],
    }


def _synthetic_span(source: Mapping[str, Any], *, workspace_id: str = "derived-workspace") -> dict[str, Any]:
    text = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")) or "source"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"schema": "evidence-span/v1", "workspace_id": workspace_id, "document_id": "derived-document", "revision_id": "derived-revision", "node_id": "derived-node", "start_codepoint": 0, "end_codepoint": len(text), "quote": text, "quote_hash": digest, "canonical_text_hash": digest}


def _source_ref(source: Mapping[str, Any], *, workspace_id: str = "derived-workspace") -> dict[str, str]:
    refs = source.get("source_refs")
    if isinstance(refs, list) and refs and isinstance(refs[0], Mapping) and set(refs[0]) == {"workspace_id", "document_id", "revision_id"}:
        return {key: str(refs[0][key]) for key in refs[0]}
    return {"workspace_id": workspace_id, "document_id": "derived-document", "revision_id": "derived-revision"}


def _derive(source: Any, source_format: str, target_format: str, *, workspace_id: str = "derived-workspace") -> dict[str, Any]:
    """Convert a validated frozen Data target; reject arbitrary/junk sources."""
    if source_format not in TARGET_SCHEMA_FILES:
        raise TargetSchemaError(f"unsupported source format: {source_format}")
    source_value = validate_target_payload(source_format, source)
    if source_format == target_format:
        return source_value
    if target_format == "character-archetype/v1":
        # Preserve useful source identity while projecting into the single
        # frozen archetype shape.  Every field is generated from validated
        # source values; no arbitrary source keys are copied through.
        source_id = str(source_value.get("archetype_id") or source_value.get("character_id") or source_value.get("template_id") or source_value.get("world_id") or "derived")
        return {
            "schema": target_format, "format_id": target_format, "archetype_id": f"derived-character-archetype-{source_id[:80]}", "version": "1.0.0",
            "dimensions": [
                {"dimension_id": "identity", "label": "Identity", "description": f"Identity projected from {source_id}", "required": True, "allowed_values": []},
                {"dimension_id": "goal", "label": "Goal", "description": "Character objective", "required": True, "allowed_values": []},
            ],
            "atom_kinds": ["identity", "goal", "motivation", "fear", "ability", "flaw", "relationship", "conflict", "arc", "event", "voice", "behavior"],
            "card_fields": ["identity", "goals", "motivations", "fears", "abilities", "flaws", "relationships", "conflicts", "arc", "key_events", "speech_habits", "behavior_evidence"],
            "relation_kinds": ["ally", "enemy", "family", "mentor", "rival", "romantic", "professional", "unknown"],
            "arc_stages": ["setup", "pressure", "turning_point", "crisis", "resolution"],
            "interpreter_mappings": expected_interpreter_mappings(target_format),
        }
    if target_format == "world-rule-template/v1":
        source_id = str(source_value.get("template_id") or source_value.get("world_id") or source_value.get("character_id") or "derived")
        statement = str(source_value.get("description") or source_value.get("identity") or source_value.get("name") or "Derived rule remains subject to Core review")
        return {
            "schema": target_format, "format_id": target_format, "template_id": f"derived-world-rules-{source_id[:80]}", "version": "1.0.0",
            "rules": [{"rule_id": "derived-rule", "statement": statement, "scope": "all", "priority": 1, "exceptions": []}],
            "invariants": ["Derived rules preserve source attribution"], "constraint_kinds": ["causality", "geography", "economy", "magic", "technology", "social"],
            "interpreter_mappings": expected_interpreter_mappings(target_format),
        }
    if target_format == "plot-structure-template/v1":
        source_id = str(source_value.get("template_id") or source_value.get("archetype_id") or source_value.get("world_id") or source_value.get("character_id") or "derived")
        description = str(source_value.get("description") or source_value.get("identity") or source_value.get("name") or "Derived deterministic plot structure")
        return {
            "schema": target_format, "format_id": target_format, "template_id": f"derived-plot-template-{source_id[:80]}", "version": "1.0.0", "scope": "chapter",
            "beats": [{"beat_id": "opening", "ordinal": 1, "label": "Opening", "purpose": description, "required": True, "children": []}],
            "chapter_template": {"opening": "Opening", "turning_point": "Turning point", "climax": "Climax", "ending": "Ending"},
            "metadata": {"description": description, "tags": ["derived", source_id[:40]]},
        }
    if target_format == "character-card/v1":
        source_id = str(source_value.get("character_id") or source_value.get("archetype_id") or source_value.get("template_id") or "derived-character")
        identity = str(source_value.get("identity") or source_value.get("description") or source_value.get("name") or "Derived identity from validated source")
        return {
            "schema": target_format, "format_id": target_format, "character_id": f"derived-character-{source_id[:80]}", "name": str(source_value.get("name") or "Derived Character"), "identity": identity,
            "goals": ["Pursue the declared objective"], "motivations": ["Protect a meaningful stake"], "fears": ["Failure"], "abilities": ["Persistence"], "flaws": ["Uncertainty"],
            "relationships": [], "conflicts": [], "arc": {"stage": "setup", "stages": ["setup"], "summary": "Derived arc", "key_events": ["Validated source was transformed"]},
            "speech_habits": ["Measured speech"], "behavior_evidence": [_synthetic_span(source_value, workspace_id=workspace_id)], "source_refs": [_source_ref(source_value, workspace_id=workspace_id)], "authority": "candidate_only",
        }
    if target_format in {"world-entry/v1", "world-entry/world", "world/v1"}:
        source_id = str(source_value.get("world_id") or source_value.get("template_id") or source_value.get("archetype_id") or "derived-world")
        description = str(source_value.get("description") or source_value.get("identity") or source_value.get("name") or "Derived world entry from validated source")
        return {
            "schema": target_format, "format_id": target_format, "world_id": f"derived-world-{source_id[:80]}", "name": str(source_value.get("name") or "Derived World"), "category": "derived", "description": description,
            "rules": [str(source_value.get("statement") or "Derived rule remains subject to review")], "constraints": ["Preserve source attribution"], "evidence_spans": [_synthetic_span(source_value, workspace_id=workspace_id)], "source_refs": [_source_ref(source_value, workspace_id=workspace_id)], "authority": "candidate_only",
        }
    raise TargetSchemaError(f"unsupported target format: {target_format}")


def _verify_bundle_payloads(bundle: Mapping[str, Any], host: HostPort | None, expected_contract: str) -> None:
    if bundle.get("contract_id") != expected_contract:
        raise AssetWorkerError("RESULT_INVALID", "result contract does not match capability")
    if host is None:
        raise AssetWorkerError("HOST_REQUIRED", "result payload verification requires HostPort")
    for item in bundle.get("items", []):
        if item.get("schema") == "candidate-item/v1":
            expected_hash = item["mutation"]["payload_hash"]
            payload_schema = item["mutation"]["payload_schema"]
        elif item.get("schema") == "artifact-item/v1":
            expected_hash = item["payload_hash"]
            payload_schema = None
        else:
            continue
        raw = _read_asset(host, item["payload_asset_id"], expected_hash, "RESULT_INVALID")
        if payload_schema is not None:
            payload = _strict_json(raw, "RESULT_INVALID")
            try:
                validate_target_payload(payload_schema, payload)
            except (TargetSchemaError, DataBundleError) as exc:
                raise AssetWorkerError("RESULT_INVALID", f"candidate payload schema/semantics invalid: {exc}") from exc
        else:
            payload = _strict_json(raw, "RESULT_INVALID")
            if not isinstance(payload, Mapping) or payload.get("schema") != "plugin-data-bundle/v1":
                raise AssetWorkerError("RESULT_INVALID", "artifact payload is not plugin-data-bundle/v1")
            # Recompute the package identity from the exact raw file Assets,
            # not merely the public bundle shape.  This keeps a tampered
            # package_hash/release_id or file table from becoming resumable;
            # the SDK remains the sole canonical identity authority.
            try:
                from plotpilot_plugin_sdk.verifier import verify_data_bundle
                verify_data_bundle(payload)
                files_by_path: dict[str, bytes] = {}
                rows = payload.get("files")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("data bundle file table is empty")
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise ValueError("data bundle file row is not an object")
                    path = row.get("path")
                    asset_id = row.get("asset_id")
                    expected_file_hash = row.get("sha256")
                    if not isinstance(path, str) or not isinstance(asset_id, str) or not isinstance(expected_file_hash, str):
                        raise ValueError("data bundle file row identity is incomplete")
                    files_by_path[path] = _read_asset(host, asset_id, expected_file_hash, "RESULT_INVALID")
                verify_data_bundle_identity(payload, version="1.0.0", files_by_path=files_by_path)
            except Exception as exc:
                raise AssetWorkerError("RESULT_INVALID", f"data bundle payload invalid: {exc}") from exc


def _bind_target_workspace(target: Mapping[str, Any], workspace_id: str) -> None:
    """Fence derived source references to the current RunSnapshot workspace.

    Target schemas intentionally do not carry a request/snapshot object.  The
    runtime therefore applies the Core workspace fence at the boundary where
    a target becomes a Candidate, while still allowing deterministic synthetic
    document/revision identifiers for cross-format projections.
    """
    for key in ("source_refs", "evidence_spans", "behavior_evidence"):
        rows = target.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise AssetWorkerError("TARGET_SCHEMA_INVALID", f"{key} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or row.get("workspace_id") != workspace_id:
                raise AssetWorkerError("TARGET_SCHEMA_INVALID", f"{key}[{index}] crosses the snapshot workspace")
    relationships = target.get("relationships")
    if isinstance(relationships, list):
        for index, relation in enumerate(relationships):
            if not isinstance(relation, Mapping):
                raise AssetWorkerError("TARGET_SCHEMA_INVALID", f"relationships[{index}] is not an object")
            for span_index, span in enumerate(relation.get("evidence_spans", [])):
                if not isinstance(span, Mapping) or span.get("workspace_id") != workspace_id:
                    raise AssetWorkerError("TARGET_SCHEMA_INVALID", f"relationships[{index}].evidence_spans[{span_index}] crosses the snapshot workspace")


class AssetDerivationPlugin:
    def __init__(self) -> None:
        self.package_hash, self.release_id = self._identity()
        self.last_stage_response: dict[str, Any] | None = None
        self.last_state: dict[str, Any] | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.last_receipt: dict[str, Any] | None = None
        self._last_receipt_id: str | None = None

    def _identity(self) -> tuple[str, str]:
        try:
            identity = load_runtime_identity() if load_runtime_identity else None
            if identity:
                return str(identity["package_hash"]), str(identity["release_id"])
        except Exception as exc:
            raise AssetWorkerError("PACKAGE_IDENTITY_ERROR", str(exc)) from exc
        raise AssetWorkerError("PACKAGE_IDENTITY_ERROR", "package identity sidecar is unavailable")

    def _stage(self, value: Mapping[str, Any], context: _Context, host: HostPort | None, bundle: Mapping[str, Any], bundle_asset_id: str) -> dict[str, Any] | None:
        if bundle["contract_id"] != "candidate-batch/v1" or host is None:
            return None
        response = _host_call(host, "host.candidate.stage/v1", {"operation_key": value["operation_key"], "result_bundle_asset_id": bundle_asset_id, "input_snapshot_hash": value["run_snapshot_hash"]})
        expected_items = [item["item_id"] for item in bundle["items"]]
        rows = response.get("staged_items")
        if response.get("accepted") is not True or not isinstance(rows, list):
            raise AssetWorkerError("HOST_REJECTED", "Core rejected candidate stage")
        if [row.get("item_id") for row in rows] != expected_items or any(not isinstance(row.get("candidate_id"), str) or row.get("stage_status") != "created" for row in rows):
            raise AssetWorkerError("HOST_CONTRACT_ERROR", "stage response does not bind staged items")
        context.stage_response = deepcopy(response)
        self.last_stage_response = deepcopy(response)
        return response

    def _persist_checkpoint(self, value: Mapping[str, Any], context: _Context, host: HostPort | None, bundle_asset_id: str, bundle_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state_unsigned = {
            "schema": "asset-resume-state/v1", "state_id": value["attempt_id"] + "-state", "plugin_id": PLUGIN_ID, "capability_id": value["capability_id"],
            "package_hash": self.package_hash, "release_id": self.release_id, "job_id": value["job_id"], "step_id": value["step_id"], "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"],
            "checkpoint_id": value["checkpoint_ids"][0], "checkpoint_seq": 1, "run_snapshot_hash": value["run_snapshot_hash"], "workspace_id": value["workspace_id"], "binding_hash": context.binding_hash,
            "result_bundle_asset_id": bundle_asset_id, "result_bundle_hash": bundle_hash,
        }
        state = {**state_unsigned, "state_hash": hash_json("asset-resume-state/v1", state_unsigned)}
        state_asset_id = _upload(host, context, _json_bytes(state), "application/json", "state")
        checkpoint_unsigned = {
            "schema": "checkpoint/v1", "checkpoint_id": value["checkpoint_ids"][0], "checkpoint_seq": 1, "job_id": value["job_id"], "step_id": value["step_id"], "source_attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"], "run_snapshot_hash": value["run_snapshot_hash"],
            "replay_policy": "checkpoint_resume", "completed_units": 1, "total_units": value["total_units"], "unit_set_hash": state["state_hash"], "state_asset_id": state_asset_id, "created_at": value["created_at"],
        }
        checkpoint = {**checkpoint_unsigned, "checkpoint_hash": hash_json("checkpoint/v1", checkpoint_unsigned)}
        checkpoint_asset_id = _upload(host, context, _json_bytes(checkpoint), "application/json", "checkpoint")
        if host is not None:
            response = _host_call(host, "host.checkpoint.commit/v1", {"operation_key": value["operation_key"], "checkpoint_asset_id": checkpoint_asset_id})
            if response.get("accepted") is not True or response.get("checkpoint_id") != checkpoint["checkpoint_id"] or response.get("completed_units") != checkpoint["completed_units"] or response.get("total_units") != checkpoint["total_units"]:
                raise AssetWorkerError("CHECKPOINT_INVALID", "checkpoint commit identity mismatch")
        self.last_state = deepcopy(state)
        self.last_checkpoint = deepcopy(checkpoint)
        return checkpoint, state

    def _complete(self, value: Mapping[str, Any], context: _Context, host: HostPort | None, outcome: str, bundle_asset_id: str | None, stage_key: str | None = None) -> dict[str, Any] | None:
        if host is None:
            return None
        context.local_seq += 1
        response = _host_call(host, "host.job.complete/v1", {"operation_key": value["operation_key"], "worker_run_id": value["worker_run_id"], "outcome": outcome, "result_bundle_asset_id": bundle_asset_id, "candidate_stage_operation_key": stage_key, "terminal_detail_asset_id": None, "local_seq": context.local_seq})
        if response.get("accepted") is not True or response.get("attempt_state") != outcome or response.get("step_state") != outcome or response.get("job_state") != outcome or response.get("provenance_receipt_id") != value["provenance_receipt_id"]:
            raise AssetWorkerError("HOST_CONTRACT_ERROR", "job completion acknowledgement drift")
        self._last_receipt_id = str(response["provenance_receipt_id"])
        return response

    def _run_derive(self, value: Mapping[str, Any], host: HostPort | None, context: _Context) -> dict[str, Any]:
        _event(value, context, host, "asset-derive-start")
        source = _load_json_asset(host, value["source_asset_id"], value["source_asset_hash"])
        projection = value.get("source_projection")
        if isinstance(projection, Mapping):
            projected_format = projection.get("source_format_id")
            if projected_format is not None and projected_format != value["source_format_id"]:
                raise AssetWorkerError("SOURCE_INVALID", "source projection format does not match request")
            projected_payload = projection.get("payload")
            if projected_payload is not None and projected_payload != source:
                raise AssetWorkerError("SOURCE_INVALID", "source projection payload does not match the Host Asset")
        target = _derive(source, value["source_format_id"], value["target_format_id"], workspace_id=value["workspace_id"])
        _bind_target_workspace(target, value["workspace_id"])
        try:
            target = validate_target_payload(value["target_format_id"], target)
        except (TargetSchemaError, DataBundleError) as exc:
            raise AssetWorkerError("TARGET_SCHEMA_INVALID", str(exc)) from exc
        data = _json_bytes(target)
        payload_asset_id = _upload(host, context, data, "application/json", "target")
        item = _candidate_item(value, data, payload_asset_id, value["target_format_id"], "derived-" + value["target_format_id"].replace("/", "-"))
        bundle = _bundle(value, CAPABILITY_TEMPLATE_DERIVE, [item], self.release_id)
        _event(value, context, host, "asset-derive-ready", payload_asset_id)
        return bundle

    def _run_package(self, value: Mapping[str, Any], host: HostPort | None, context: _Context) -> dict[str, Any]:
        if host is None:
            raise AssetWorkerError("HOST_REQUIRED", "Data package identity requires HostPort")
        _event(value, context, host, "data-package-start")
        rows: list[dict[str, Any]] = []
        files_by_path: dict[str, bytes] = {}
        for raw_row in value["files"]:
            row = dict(raw_row)
            raw = _read_asset(host, row["asset_id"], row["sha256"])
            if len(raw) != row["size"]:
                raise AssetWorkerError("PACKAGE_INVALID", f"file size mismatch: {row['path']}")
            if row["path"] in files_by_path:
                raise AssetWorkerError("PACKAGE_INVALID", f"duplicate package path: {row['path']}")
            files_by_path[row["path"]] = raw
            rows.append(row)
        try:
            digest = independent_package_identity(files_by_path, value["data_plugin_id"], value["version"])
        except Exception as exc:
            raise AssetWorkerError("PACKAGE_INVALID", f"SDK package identity failed: {exc}") from exc
        if digest.package_hash != value["package_hash"] or digest.release_id != value["release_id"]:
            raise AssetWorkerError("PACKAGE_INVALID", "declared package/release identity differs from SDK recomputation")
        try:
            bundle = build_data_bundle(data_plugin_id=value["data_plugin_id"], data_release_id=value["release_id"], package_hash=value["package_hash"], format_id=value["format_id"], root_path=value["root_path"], files=rows)
            verify_data_bundle_identity(bundle, version=value["version"], files_by_path=files_by_path)
        except DataBundleError as exc:
            raise AssetWorkerError("PACKAGE_INVALID", str(exc)) from exc
        data = _json_bytes(bundle)
        payload_asset_id = _upload(host, context, data, "application/json", "data-bundle")
        item = _artifact_item(value, payload_asset_id, hashlib.sha256(data).hexdigest(), "data-bundle-" + value["format_id"].replace("/", "-"))
        result = _bundle(value, CAPABILITY_DATA_PACKAGE, [item], self.release_id, "artifact-bundle/v1")
        _event(value, context, host, "data-package-ready", payload_asset_id)
        return result

    def _resume(self, value: Mapping[str, Any], capability: str, context: _Context, host: HostPort | None) -> dict[str, Any]:
        checkpoint_raw = _read_asset(host, value["resume_checkpoint_asset_id"], value["resume_checkpoint_asset_hash"], "RESUME_INVALID")
        checkpoint = _strict_json(checkpoint_raw, "RESUME_INVALID")
        if verify_checkpoint is not None:
            try:
                verify_checkpoint(checkpoint, expected_snapshot_hash=value["run_snapshot_hash"])
            except Exception as exc:
                raise AssetWorkerError("RESUME_INVALID", str(exc)) from exc
        if not isinstance(checkpoint, Mapping) or checkpoint.get("checkpoint_id") not in value["checkpoint_ids"] or checkpoint.get("state_asset_id") != value["resume_state_asset_id"] or checkpoint.get("job_id") != value["job_id"] or checkpoint.get("step_id") != value["step_id"] or checkpoint.get("source_attempt_id") != value["attempt_id"] or checkpoint.get("lease_epoch") != value["lease_epoch"] or checkpoint.get("run_snapshot_hash") != value["run_snapshot_hash"] or checkpoint.get("total_units") != value["total_units"] or checkpoint.get("completed_units") != value["total_units"]:
            raise AssetWorkerError("RESUME_INVALID", "checkpoint exact binding failed")
        state_raw = _read_asset(host, value["resume_state_asset_id"], value["resume_state_asset_hash"], "RESUME_INVALID")
        state = _strict_json(state_raw, "RESUME_INVALID")
        required = {"schema", "state_id", "plugin_id", "capability_id", "package_hash", "release_id", "job_id", "step_id", "attempt_id", "lease_epoch", "checkpoint_id", "checkpoint_seq", "run_snapshot_hash", "workspace_id", "binding_hash", "result_bundle_asset_id", "result_bundle_hash", "state_hash"}
        if not isinstance(state, Mapping) or set(state) != required:
            raise AssetWorkerError("RESUME_INVALID", "resume state fields are not closed")
        expected = {"schema": "asset-resume-state/v1", "plugin_id": PLUGIN_ID, "capability_id": capability, "package_hash": self.package_hash, "release_id": self.release_id, "job_id": value["job_id"], "step_id": value["step_id"], "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"], "checkpoint_id": checkpoint["checkpoint_id"], "checkpoint_seq": checkpoint["checkpoint_seq"], "run_snapshot_hash": value["run_snapshot_hash"], "workspace_id": value["workspace_id"], "binding_hash": context.binding_hash}
        if state.get("state_id") != value["attempt_id"] + "-state" or any(state.get(key) != expected_value for key, expected_value in expected.items()):
            raise AssetWorkerError("RESUME_INVALID", "resume state plugin/capability/package/release/binding drift")
        if checkpoint.get("unit_set_hash") != state["state_hash"]:
            raise AssetWorkerError("RESUME_INVALID", "checkpoint unit_set_hash does not bind state")
        if hashlib.sha256(state_raw).hexdigest() != value["resume_state_asset_hash"]:
            raise AssetWorkerError("RESUME_INVALID", "resume state Asset hash mismatch")
        if state["state_hash"] != hash_json("asset-resume-state/v1", {key: item for key, item in state.items() if key != "state_hash"}):
            raise AssetWorkerError("RESUME_INVALID", "resume state hash mismatch")
        bundle_raw = _read_asset(host, state["result_bundle_asset_id"], state["result_bundle_hash"], "RESUME_INVALID")
        bundle = _strict_json(bundle_raw, "RESUME_INVALID")
        if bundle_raw != _json_bytes(bundle):
            raise AssetWorkerError("RESUME_INVALID", "resume Bundle not canonical")
        if verify_result_bundle is not None:
            try:
                verify_result_bundle(bundle, snapshot_workspace_id=value["workspace_id"] if bundle.get("contract_id") == "candidate-batch/v1" else None, snapshot_hash_value=value["run_snapshot_hash"])
            except Exception as exc:
                raise AssetWorkerError("RESUME_INVALID", str(exc)) from exc
        producer = {"plugin_id": PLUGIN_ID, "release_id": self.release_id, "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"], "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"]}
        if bundle.get("producer") != producer or bundle.get("contract_id") != SPEC_BY_CAPABILITY[capability].result_contract:
            raise AssetWorkerError("RESUME_INVALID", "result producer/contract drift")
        _verify_bundle_payloads(bundle, host, SPEC_BY_CAPABILITY[capability].result_contract)
        self.last_state = deepcopy(dict(state))
        self.last_checkpoint = deepcopy(dict(checkpoint))
        stage = self._stage(value, context, host, bundle, state["result_bundle_asset_id"])
        self._complete(value, context, host, "succeeded", state["result_bundle_asset_id"], value["operation_key"] if stage is not None else None)
        self._set_receipt(value, capability, bundle, state["result_bundle_hash"], stage is not None)
        return dict(bundle)

    def _set_receipt(self, value: Mapping[str, Any], capability: str, bundle: Mapping[str, Any], bundle_hash: str, staged: bool) -> None:
        unsigned = {"schema": "provenance-receipt/v1", "receipt_id": value["provenance_receipt_id"], "plugin_id": PLUGIN_ID, "release_id": self.release_id, "package_hash": self.package_hash, "capability_id": capability, "job_id": value["job_id"], "step_id": value["step_id"], "attempt_id": value["attempt_id"], "lease_epoch": value["lease_epoch"], "run_snapshot_hash": value["run_snapshot_hash"], "bundle_id": bundle["bundle_id"], "bundle_hash": bundle_hash, "parent_receipt_ids": [], "model_receipt_ids": [], "skill_chain_result_refs": [], "staged_items": [item["item_id"] for item in bundle["items"]] if staged else [], "created_at": value["created_at"]}
        self.last_receipt = {**unsigned, "receipt_hash": hash_json("provenance-receipt/v1", unsigned)}

    def _failure(self, value: Mapping[str, Any] | None, capability: str | None, context: _Context | None, host: HostPort | None, error: AssetWorkerError) -> dict[str, Any] | None:
        if capability not in CAPABILITIES:
            return None
        base = dict(value) if isinstance(value, Mapping) else {}
        required = {"attempt_id", "job_id", "step_id", "lease_epoch", "run_snapshot_hash", "provenance_receipt_id", "workspace_id", "operation_key"}
        if not required <= set(base):
            self.last_stage_response = None
            return None
        bundle = _bundle(base, capability, [{"schema": "diagnostic-item/v1", "item_id": f"{capability.replace('/', '-')}-{base['attempt_id']}-diagnostic", "severity": "error", "code": error.code, "message": str(error), "details_asset_id": None, "details_hash": None, "source_refs": [], "status": "complete"}], self.release_id, "diagnostic-bundle/v1")
        try:
            if context is not None:
                raw = _json_bytes(bundle)
                asset_id = _upload(host, context, raw, "application/json", "failure")
                self._complete(base, context, host, "failed", asset_id, None)
        except Exception:
            pass
        self.last_stage_response = None
        self.last_receipt = None
        return bundle

    def run(self, request: Mapping[str, Any] | None, host: HostPort | None = None) -> dict[str, Any] | None:
        capability: str | None = None
        value: dict[str, Any] | None = None
        context: _Context | None = None
        try:
            if not isinstance(request, Mapping):
                raise AssetWorkerError("INPUT_INVALID", "request must object")
            value = dict(request)
            capability = value.get("capability_id") or next((spec.capability_id for spec in SPEC_BY_CAPABILITY.values() if spec.input_schema == value.get("schema")), None)
            if capability not in CAPABILITIES:
                raise AssetWorkerError("CAPABILITY_UNKNOWN", "unknown asset capability")
            value = _validate_request(value, capability)
            context = _context(value, capability)
            operation = value["operation"]
            if operation == "cancel":
                self._complete(value, context, host, "cancelled", None, None)
                return None
            if operation == "resume":
                return self._resume(value, capability, context, host)
            bundle = self._run_derive(value, host, context) if capability == CAPABILITY_TEMPLATE_DERIVE else self._run_package(value, host, context)
            _verify_bundle_payloads(bundle, host, SPEC_BY_CAPABILITY[capability].result_contract)
            if operation == "validate":
                return bundle
            raw = _json_bytes(bundle)
            bundle_asset_id = _upload(host, context, raw, "application/json", "result")
            if verify_result_bundle is not None:
                try:
                    verify_result_bundle(bundle, snapshot_workspace_id=value["workspace_id"] if bundle.get("contract_id") == "candidate-batch/v1" else None, snapshot_hash_value=value["run_snapshot_hash"])
                except Exception as exc:
                    raise AssetWorkerError("RESULT_INVALID", str(exc)) from exc
            _event(value, context, host, "result-ready", bundle_asset_id)
            self._persist_checkpoint(value, context, host, bundle_asset_id, hashlib.sha256(raw).hexdigest())
            stage = self._stage(value, context, host, bundle, bundle_asset_id)
            self._complete(value, context, host, "succeeded", bundle_asset_id, value["operation_key"] if stage is not None else None)
            self._set_receipt(value, capability, bundle, hashlib.sha256(raw).hexdigest(), stage is not None)
            return bundle
        except AssetWorkerError as exc:
            # Strict request validation can fail before ``_context`` is
            # normally created (for example an interpreter mapping order
            # drift).  If the durable attempt identity is still present,
            # derive the same transport context so the failed diagnostic is
            # acknowledged by Core; incomplete/foreign requests remain
            # side-effect free.
            if context is None and capability in CAPABILITIES and isinstance(value, Mapping):
                required = {
                    "operation_key", "worker_run_id", "provenance_receipt_id", "attempt_id",
                    "job_id", "step_id", "lease_epoch", "run_snapshot_hash", "workspace_id",
                }
                if required <= set(value):
                    try:
                        context = _context(value, capability)
                    except Exception:
                        context = None
            return self._failure(value or request, capability, context, host, exc)
        except (AssetContractError, Exception) as exc:
            error = AssetWorkerError("INPUT_INVALID" if isinstance(exc, AssetContractError) else "INTERNAL_ERROR", str(exc))
            if context is None and capability in CAPABILITIES and isinstance(value, Mapping):
                required = {
                    "operation_key", "worker_run_id", "provenance_receipt_id", "attempt_id",
                    "job_id", "step_id", "lease_epoch", "run_snapshot_hash", "workspace_id",
                }
                if required <= set(value):
                    try:
                        context = _context(value, capability)
                    except Exception:
                        context = None
            return self._failure(value or request, capability, context, host, error)


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    capability = capability_id or CAPABILITY_TEMPLATE_DERIVE
    plugin = AssetDerivationPlugin()
    return SPEC_BY_CAPABILITY[capability].descriptor(plugin.release_id)


PACKAGE_HASH, RELEASE_ID = AssetDerivationPlugin()._identity()
DESCRIPTORS = descriptors(RELEASE_ID)
_RUNTIME = AssetDerivationPlugin()


def main(request: Mapping[str, Any] | None = None, host: HostPort | None = None) -> Any:
    return capability_descriptor() if request is None else _RUNTIME.run(request, host)


__all__ = [
    "CAPABILITIES", "CAPABILITY_TEMPLATE_DERIVE", "CAPABILITY_DATA_PACKAGE", "AssetDerivationPlugin",
    "AssetWorkerError", "TerminalContractError", "PACKAGE_HASH", "RELEASE_ID", "DESCRIPTORS",
    "capability_descriptor", "main",
]
