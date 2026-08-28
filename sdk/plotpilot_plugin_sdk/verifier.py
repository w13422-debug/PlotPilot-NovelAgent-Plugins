"""Strict Python implementation of the PlotPilot v1 contract surface.

The module deliberately keeps JSON-Schema validation and the cross-object
rules in one place. The schemas reject shape drift; the semantic checks below
reject identity, ordering, fencing and hash drift before a Core mutation can
be considered durable.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, ValidationError

from .canonical import canonical_bytes, hash_jcs, normalize_snapshot, parse_json_bytes, sha256_hex
from .errors import ContractError, ContractValidationError, ErrorCode
from .package import (
    build_files_sha256,
    normalize_relative_path,
    package_hash,
    release_id,
    skill_package_hash,
    skill_release_id,
    unicode_nfc_casefold as _frozen_unicode_nfc_casefold,
)
from .rpc import HOST_METHODS, METHOD_MATRIX, WORKER_METHODS
from ._workspace import WORKSPACE_ROOT


ROOT = WORKSPACE_ROOT
SCHEMA_DIR = ROOT / "contracts" / "json-schema"

EXPECTED_WORKER_METHODS = (
    "runtime.handshake",
    "runtime.health",
    "runtime.heartbeat",
    "capability.describe",
    "settings.validate",
    "migration.plan",
    "migration.apply",
    "migration.verify",
    "job.start",
    "job.resume",
    "job.pause",
    "job.cancel",
    "runtime.shutdown",
)
EXPECTED_HOST_METHODS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.model.invoke/v1",
    "host.capability.invoke/v1",
    "host.capability.poll/v1",
    "host.capability.cancel/v1",
    "host.candidate.stage/v1",
    "host.checkpoint.commit/v1",
    "host.stream.commit/v1",
    "host.job.event/v1",
    "host.job.await_user/v1",
    "host.job.complete/v1",
    "host.log/v1",
    "host.migration.lease.renew/v1",
    "host.migration.lease.release/v1",
)
EXPECTED_ERROR_CODES = {
    1001: "incompatible_generation",
    1002: "stale_lease",
    1003: "cancelled",
    1004: "deadline_exceeded",
    1005: "asset_error",
    1006: "settings_invalid",
    1007: "migration_failed",
    1008: "duplicate_request",
    1009: "uncertain_external_effect",
    1010: "invalid_transition",
    1011: "result_contract_mismatch",
    1012: "data_interpreter_unavailable",
    1013: "release_retiring",
    1014: "checkpoint_invalid",
}

SCHEMA_NAME_ALIASES = {
    p.stem.removesuffix(".schema").replace("-v1", "/v1", 1): p.name
    for p in SCHEMA_DIR.glob("*.schema.json")
}
SCHEMA_NAME_ALIASES.update(
    {
        "plotpilot-plugin/v1": "plugin-manifest-v1.schema.json",
        "plotpilot-skill/v1": "skill-manifest-v1.schema.json",
        "rpc-envelope/v1": "rpc-envelope-v1.schema.json",
        "backup-bundle/v1": "backup-bundle-v1.schema.json",
    }
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_INPUT_FIELDS = ("schema", "source_revision_id", "ordered_atoms")
_CLAIM_INPUT_ATOM_FIELDS = (
    "ordinal",
    "atom_id",
    "payload_hash",
    "acceptance_ordinal",
    "evidence_spans",
)
AcceptedAtomSource = (
    Mapping[str, Mapping[str, Any]]
    | Sequence[Mapping[str, Any]]
    | Callable[[str], Mapping[str, Any] | None]
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str
    validator: str | None = None


def _schema_path(contract_id: str) -> Path:
    filename = SCHEMA_NAME_ALIASES.get(contract_id)
    if filename is None:
        filename = contract_id.replace("/", "-") + ".schema.json"
    path = SCHEMA_DIR / filename
    if not path.exists():
        raise ContractValidationError(f"unknown contract schema: {contract_id}")
    return path


def _load_schema(contract_id: str) -> dict[str, Any]:
    return json.loads(_schema_path(contract_id).read_text(encoding="utf-8"))


def _path(error: ValidationError) -> str:
    return "/" + "/".join(str(part) for part in error.absolute_path)


def schema_errors(contract_id: str, value: Any) -> list[ValidationIssue]:
    validator = Draft202012Validator(_load_schema(contract_id))
    errors = sorted(validator.iter_errors(value), key=lambda e: (tuple(e.absolute_path), e.message))
    return [ValidationIssue(_path(error), error.message, error.validator) for error in errors]


def validate_contract(contract_id: str, value: Any) -> list[ValidationIssue]:
    return schema_errors(contract_id, value)


def assert_valid(contract_id: str, value: Any) -> None:
    issues = validate_contract(contract_id, value)
    if issues:
        first = issues[0]
        raise ContractValidationError(
            f"{contract_id}: {first.message}",
            path=first.path,
            details=[issue.__dict__ for issue in issues],
        )


def _assert_hash(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise ContractValidationError(f"{label} mismatch: expected {expected}, got {actual}")


def _utf8_sort(values: Iterable[str]) -> list[str]:
    return sorted(values, key=lambda value: value.encode("utf-8"))


def unicode_nfc_casefold(value: str) -> str:
    """Return the cross-runtime path identity from the frozen v1 table."""
    if not isinstance(value, str):
        raise ContractValidationError("casefold identity requires a string")
    return _frozen_unicode_nfc_casefold(value)


def _casefold_unique(values: Iterable[str], message: str) -> None:
    _assert_unique((unicode_nfc_casefold(value) for value in values), message)


def _key_tuple(value: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    return tuple("" if value.get(field) is None else str(value.get(field)) for field in fields)


def _assert_unique(values: Iterable[Any], message: str) -> None:
    values_list = list(values)
    try:
        unique = len(values_list) == len(set(values_list))
    except TypeError as exc:
        raise ContractValidationError(message) from exc
    if not unique:
        raise ContractValidationError(message)


def hash_without_field(value: Mapping[str, Any], field: str, prefix: str) -> str:
    unsigned = {key: copy.deepcopy(item) for key, item in value.items() if key != field}
    return hash_jcs(prefix, unsigned)


def request_key_bytes(snapshot: Mapping[str, Any], *, parameters_asset_sha256: str | None = None) -> bytes:
    scope = snapshot["scope"]
    params_hash = parameters_asset_sha256
    if params_hash is None:
        asset_map = {item["asset_id"]: item["sha256"] for item in snapshot.get("asset_hashes", [])}
        params_id = snapshot.get("parameters_asset_id")
        params_hash = asset_map.get(params_id, "-") if params_id else "-"
    revisions = sorted(snapshot.get("input_revisions", []), key=lambda item: (item["document_id"], item["revision_id"]))
    revision_line = ",".join(f"{item['document_id']}={item['revision_id']}={item['content_hash']}" for item in revisions)
    lines = [
        "request-key/v1",
        snapshot["workspace_id"],
        scope["operation"],
        scope["document_id"] or "null",
        scope["node_id"] or "null",
        revision_line,
        snapshot["plan_revision_id"],
        params_hash,
        snapshot["run_intent_id"],
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def request_key(snapshot: Mapping[str, Any], *, parameters_asset_sha256: str | None = None) -> str:
    return sha256_hex(request_key_bytes(snapshot, parameters_asset_sha256=parameters_asset_sha256))


def normalize_snapshot_for_hash(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_snapshot(dict(snapshot))


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return hash_jcs(
        "run-snapshot/v1",
        normalize_snapshot_for_hash({key: value for key, value in snapshot.items() if key != "snapshot_hash"}),
    )


def verify_manifest(manifest: Mapping[str, Any]) -> None:
    assert_valid("plugin-manifest/v1", manifest)
    capability_ids = [item["capability_id"] for item in manifest["capabilities"]]
    _assert_unique(capability_ids, "manifest capability IDs must be unique")
    for capability in manifest["capabilities"]:
        _assert_unique(capability["operations"], "manifest capability operations must be unique")
    compatibility = manifest["compatibility"]
    if compatibility.get("core_api") != ">=1.0 <2.0" or compatibility.get("plugin_rpc") != "1" or compatibility.get("ui_host") != "1":
        raise ContractValidationError("manifest compatibility is outside the v1 matrix")
    if manifest["kind"] == "data" and manifest["needs"]:
        raise ContractValidationError("data plugin needs must be empty")
    if manifest["kind"] == "data" and any(key in manifest for key in ("backend", "storage", "ui")):
        raise ContractValidationError("data plugin cannot declare backend/storage/UI fields")
    ui = manifest.get("ui")
    if ui is not None:
        _assert_unique(
            ((item["contribution_id"], item["slot"], item["capability_id"]) for item in ui["contributions"]),
            "UI contribution triples must be unique",
        )
        capability_set = set(capability_ids)
        for contribution in ui["contributions"]:
            if contribution["capability_id"] not in capability_set:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI contribution references an unknown capability")


def _catalog_interpreter_map(catalog: Mapping[str, Any]) -> dict[str, set[tuple[str, str]]]:
    """Return the canonical ``format -> exact (plugin, capability)`` map."""

    formats: dict[str, set[tuple[str, str]]] = {}
    for data_format in catalog.get("data_formats", []):
        format_id = data_format.get("format_id")
        if not isinstance(format_id, str):
            raise ContractValidationError("catalog data format has no format_id")
        if format_id in formats:
            raise ContractValidationError(f"catalog contains duplicate data format: {format_id}")
        pairs: set[tuple[str, str]] = set()
        for interpreter in data_format.get("interpreters", []):
            plugin_id = interpreter.get("plugin_id")
            capability_id = interpreter.get("capability_id")
            if not isinstance(plugin_id, str) or not isinstance(capability_id, str):
                raise ContractValidationError(f"catalog interpreter for {format_id} is not an exact pair")
            pair = (plugin_id, capability_id)
            if pair in pairs:
                raise ContractValidationError(f"catalog repeats interpreter pair for {format_id}: {pair}")
            pairs.add(pair)
        formats[format_id] = pairs
    return formats


def verify_data_interpreter_binding(
    format_id: str,
    interpreter_plugin_id: str,
    interpreter_capability_id: str,
    *,
    catalog: Mapping[str, Any],
) -> None:
    """Verify one Data format against its exact interpreter capability.

    The opaque Data binding ID in ``plugin-plan/v1`` is resolved by Core.  At
    the contract boundary the resolved identity is always this exact
    ``{plugin_id, capability_id}`` pair; accepting only a plugin ID would
    allow a plugin to claim a format through the wrong capability.
    """

    if not all(isinstance(value, str) and value for value in (format_id, interpreter_plugin_id, interpreter_capability_id)):
        raise ContractValidationError("Data interpreter binding must contain non-empty string identities")
    interpreter_map = _catalog_interpreter_map(catalog)
    expected = interpreter_map.get(format_id)
    if expected is None:
        raise ContractError(ErrorCode.DATA_INTERPRETER_UNAVAILABLE, f"no interpreter for {format_id}")
    actual = (interpreter_plugin_id, interpreter_capability_id)
    if actual not in expected:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"interpreter {actual!r} does not accept Data format {format_id}",
        )


def verify_catalog(catalog: Mapping[str, Any]) -> None:
    """Verify the frozen catalog's cross-object and count invariants.

    ``plugin-catalog-v1.json`` is a registry projection rather than a second
    JSON-Schema authority.  This check therefore validates the relationships
    that JSON Schema cannot express: bidirectional Data interpreter mapping,
    capability/UI contribution ownership, headless/UI exclusion, and the
    published count fields.
    """

    if catalog.get("schema") != "novel-agent-plugin-catalog/v1":
        raise ContractValidationError("catalog schema mismatch")
    plugins = catalog.get("code_plugins")
    if not isinstance(plugins, list):
        raise ContractValidationError("catalog code_plugins must be an array")
    plugin_ids = [item.get("plugin_id") for item in plugins]
    if any(not isinstance(plugin_id, str) for plugin_id in plugin_ids):
        raise ContractValidationError("catalog plugin IDs must be strings")
    _assert_unique(plugin_ids, "catalog plugin IDs must be unique")

    expected_statuses = {"planned", "conditional", "not_planned_duplicate"}
    planned = conditional = duplicate = 0
    capability_ids: list[str] = []
    capability_pairs: set[tuple[str, str]] = set()
    contribution_triples: set[tuple[str, str, str]] = set()
    all_contribution_slots: set[str] = set()
    registered_slots = {
        item.get("slot_id")
        for item in catalog.get("ui_slots", [])
        if isinstance(item, Mapping) and isinstance(item.get("slot_id"), str)
    }
    if len(registered_slots) != len(catalog.get("ui_slots", [])):
        raise ContractValidationError("catalog UI slot IDs must be unique and non-empty")

    for plugin in plugins:
        plugin_id = plugin["plugin_id"]
        status = plugin.get("status")
        if status not in expected_statuses:
            raise ContractValidationError(f"catalog plugin has unknown status: {plugin_id}")
        planned += status == "planned"
        conditional += status == "conditional"
        duplicate += status == "not_planned_duplicate"
        capabilities = plugin.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ContractValidationError(f"catalog capabilities must be an array: {plugin_id}")
        plugin_capability_ids = [item.get("capability_id") for item in capabilities]
        if any(not isinstance(capability_id, str) for capability_id in plugin_capability_ids):
            raise ContractValidationError(f"catalog capability IDs must be strings: {plugin_id}")
        _assert_unique(plugin_capability_ids, f"catalog capability IDs must be unique: {plugin_id}")
        plugin_slots = plugin.get("ui_slots", [])
        if not isinstance(plugin_slots, list) or any(not isinstance(slot, str) for slot in plugin_slots):
            raise ContractValidationError(f"catalog plugin UI slots are invalid: {plugin_id}")
        _assert_unique(plugin_slots, f"catalog plugin UI slots must be unique: {plugin_id}")
        contribution_slots: set[str] = set()
        for capability in capabilities:
            capability_id = capability["capability_id"]
            capability_ids.append(capability_id)
            pair = (plugin_id, capability_id)
            if pair in capability_pairs:
                raise ContractValidationError(f"catalog capability pair is duplicated: {pair}")
            capability_pairs.add(pair)
            accepted_formats = capability.get("accepted_data_formats", [])
            if not isinstance(accepted_formats, list):
                raise ContractValidationError(f"accepted_data_formats must be an array: {capability_id}")
            _assert_unique(accepted_formats, f"accepted_data_formats must be unique: {capability_id}")
            contributions = capability.get("ui_contributions", [])
            if not isinstance(contributions, list):
                raise ContractValidationError(f"ui_contributions must be an array: {capability_id}")
            headless = capability.get("headless")
            if not isinstance(headless, bool):
                raise ContractValidationError(f"catalog capability headless flag is missing: {capability_id}")
            if headless and contributions:
                raise ContractValidationError(f"headless capability declares UI contributions: {capability_id}")
            if not headless and not contributions:
                raise ContractValidationError(f"non-headless capability has no UI contribution: {capability_id}")
            for contribution in contributions:
                contribution_id = contribution.get("contribution_id")
                slot = contribution.get("slot")
                bound_capability_id = contribution.get("capability_id")
                triple = (contribution_id, slot, bound_capability_id)
                if not all(isinstance(value, str) and value for value in triple):
                    raise ContractValidationError(f"invalid UI contribution for {capability_id}")
                if triple in contribution_triples:
                    raise ContractValidationError(f"duplicate UI contribution triple: {triple}")
                contribution_triples.add(triple)
                if bound_capability_id != capability_id:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI contribution capability binding is not its owner")
                if slot not in registered_slots:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"UI contribution uses an unknown slot: {slot}")
                contribution_slots.add(slot)
                all_contribution_slots.add(slot)
        if set(plugin_slots) != contribution_slots:
            raise ContractValidationError(f"plugin UI slots do not match its capability contributions: {plugin_id}")

    _assert_unique(capability_ids, "catalog capability IDs must be globally unique")
    if catalog.get("code_plugin_count") != len(plugins):
        raise ContractValidationError("catalog code_plugin_count is incorrect")
    if catalog.get("planned_code_plugin_count") != planned:
        raise ContractValidationError("catalog planned_code_plugin_count is incorrect")
    if catalog.get("conditional_code_plugin_count") != conditional:
        raise ContractValidationError("catalog conditional_code_plugin_count is incorrect")
    if catalog.get("not_planned_duplicate_count") != duplicate:
        raise ContractValidationError("catalog not_planned_duplicate_count is incorrect")
    if catalog.get("capability_count") != len(capability_ids):
        raise ContractValidationError("catalog capability_count is incorrect")

    plan_rules = catalog.get("plan_rules")
    if not isinstance(plan_rules, Mapping):
        raise ContractValidationError("catalog plan_rules are missing")
    if plan_rules.get("result_modes") != ["separate", "synthesize"]:
        raise ContractValidationError("catalog plan_rules must expose only separate/synthesize")
    if plan_rules.get("separate_requires_null_synthesizer") is not True or plan_rules.get("synthesize_requires_non_null_synthesizer_resolving_enabled_binding") is not True:
        raise ContractValidationError("catalog synthesizer lifecycle rules are incomplete")
    result_rules = catalog.get("result_contract_rules")
    if not isinstance(result_rules, Mapping) or result_rules.get("allowed") != ["artifact-bundle/v1", "candidate-batch/v1", "diagnostic-bundle/v1"] or result_rules.get("failed_or_skipped_create_candidate") is not False or result_rules.get("failed_attempt_bundle") != ["null", "diagnostic-bundle/v1"] or result_rules.get("stream_is_host_behavior_not_result_contract") is not True:
        raise ContractValidationError("catalog result contract rules are incomplete")
    rebind_rules = catalog.get("evidence_rebind_rules")
    if rebind_rules != {
        "inspect_capability": "source.evidence.rebind.inspect/v1",
        "inspect_result": "diagnostic-bundle/v1",
        "propose_capability": "source.evidence.rebind.propose/v1",
        "propose_result": "candidate-batch/v1",
        "unchanged_scope": "same_revision_no_op_only",
        "cross_revision_classes": ["rebound", "needs_rerun", "orphaned"],
    }:
        raise ContractValidationError("catalog evidence rebind rules are incomplete")

    declared = _catalog_interpreter_map(catalog)
    accepted: dict[str, set[tuple[str, str]]] = {}
    for plugin in plugins:
        for capability in plugin.get("capabilities", []):
            for format_id in capability.get("accepted_data_formats", []):
                accepted.setdefault(format_id, set()).add((plugin["plugin_id"], capability["capability_id"]))
    if accepted != declared:
        missing = {key: sorted(value) for key, value in (accepted.keys() | declared.keys()) if accepted.get(key, set()) - declared.get(key, set())}
        extra = {key: sorted(value) for key, value in (accepted.keys() | declared.keys()) if declared.get(key, set()) - accepted.get(key, set())}
        raise ContractValidationError(f"Data interpreter mapping is not bidirectional: missing={missing}, extra={extra}")


def verify_data_bundle(bundle: Mapping[str, Any]) -> None:
    assert_valid("plugin-data-bundle/v1", bundle)
    _assert_hash(hash_without_field(bundle, "bundle_hash", "plugin-data-bundle/v1"), bundle["bundle_hash"], "bundle_hash")
    normalized_paths = [normalize_relative_path(item["path"]) for item in bundle["files"]]
    if normalized_paths != _utf8_sort(normalized_paths):
        raise ContractValidationError("data bundle files must be sorted by normalized UTF-8 path")
    _casefold_unique(normalized_paths, "data bundle paths must be NFC/casefold-unique")
    _assert_unique((item["asset_id"] for item in bundle["files"]), "data bundle asset IDs must be unique")
    if normalize_relative_path(bundle["root_path"]) not in normalized_paths:
        raise ContractValidationError("data bundle root_path is absent from files")


def _assert_id(value: Any, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{label} must be a PlotPilot ID")


def _assert_hash_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{label} must be lowercase SHA-256")


def verify_evidence_span(
    span: Mapping[str, Any],
    canonical_text: str | None = None,
    node_range: Mapping[str, int] | None = None,
    *,
    expected_workspace_id: str | None = None,
    expected_document_id: str | None = None,
    expected_revision_id: str | None = None,
    expected_node_id: str | None = None,
    expected_canonical_text_hash: str | None = None,
) -> None:
    """Verify the cross-language ``evidence-span/v1`` semantic contract.

    This contract is intentionally checked here instead of adding a second
    schema authority: the frozen manifest predates the EvidenceSpan vertical
    slice.  Python ``str`` iteration and TypeScript ``[...text]`` both count
    Unicode scalar values, so the exact slice and hash formula are shared.
    """

    if not isinstance(span, Mapping):
        raise ContractValidationError("EvidenceSpan must be an object")
    _assert_exact_fields(span, ("schema", "workspace_id", "document_id", "revision_id", "node_id", "start_codepoint", "end_codepoint", "quote", "quote_hash", "canonical_text_hash"), "EvidenceSpan")
    if span["schema"] != "evidence-span/v1":
        raise ContractValidationError("EvidenceSpan schema mismatch")
    for field in ("workspace_id", "document_id", "revision_id", "node_id"):
        _assert_id(span[field], f"EvidenceSpan.{field}")
    for field, expected in (
        ("workspace_id", expected_workspace_id),
        ("document_id", expected_document_id),
        ("revision_id", expected_revision_id),
        ("node_id", expected_node_id),
    ):
        if expected is not None and span[field] != expected:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"EvidenceSpan {field} does not match its source identity")
    start = span["start_codepoint"]
    end = span["end_codepoint"]
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        raise ContractValidationError("EvidenceSpan range must be non-negative scalar offsets")
    if not isinstance(span["quote"], str):
        raise ContractValidationError("EvidenceSpan quote must be a string")
    _assert_hash_string(span["quote_hash"], "EvidenceSpan.quote_hash")
    _assert_hash_string(span["canonical_text_hash"], "EvidenceSpan.canonical_text_hash")
    if sha256_hex(span["quote"].encode("utf-8")) != span["quote_hash"]:
        raise ContractValidationError("EvidenceSpan quote_hash does not match quote UTF-8 bytes")
    if expected_canonical_text_hash is not None:
        _assert_hash_string(expected_canonical_text_hash, "expected_canonical_text_hash")
        if span["canonical_text_hash"] != expected_canonical_text_hash:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "EvidenceSpan canonical text hash does not match the Revision")
    if canonical_text is not None:
        if not isinstance(canonical_text, str):
            raise ContractValidationError("canonical_text must be a string")
        if end > len(canonical_text):
            raise ContractValidationError("EvidenceSpan range exceeds canonical text scalar length")
        if canonical_text[start:end] != span["quote"]:
            raise ContractValidationError("EvidenceSpan quote is not the exact canonical scalar slice")
        text_hash = sha256_hex(canonical_text.encode("utf-8"))
        if span["canonical_text_hash"] != text_hash:
            raise ContractValidationError("EvidenceSpan canonical_text_hash does not match canonical text UTF-8 bytes")
    if node_range is not None:
        if not isinstance(node_range, Mapping) or set(node_range) != {"start_codepoint", "end_codepoint"}:
            raise ContractValidationError("node_range must contain exactly start_codepoint/end_codepoint")
        node_start = node_range["start_codepoint"]
        node_end = node_range["end_codepoint"]
        if isinstance(node_start, bool) or isinstance(node_end, bool) or not isinstance(node_start, int) or not isinstance(node_end, int) or node_start < 0 or node_end < node_start:
            raise ContractValidationError("node_range is invalid")
        if start < node_start or end > node_end:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "EvidenceSpan is outside its Node range")


def verify_claim_input_structure(
    claim_input: Mapping[str, Any],
    *,
    expected_source_revision_id: str | None = None,
    canonical_text: str | None = None,
    expected_canonical_text_hash: str | None = None,
) -> None:
    """Verify only the closed structural/evidence shape of claim-input/v1."""

    if not isinstance(claim_input, Mapping):
        raise ContractValidationError("claim-input must be an object")
    _assert_exact_fields(claim_input, _CLAIM_INPUT_FIELDS, "claim-input/v1")
    if claim_input["schema"] != "claim-input/v1":
        raise ContractValidationError("claim-input schema mismatch")
    _assert_id(claim_input["source_revision_id"], "claim-input.source_revision_id")
    if expected_source_revision_id is not None and claim_input["source_revision_id"] != expected_source_revision_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input source Revision does not match the run")
    ordered_atoms = claim_input["ordered_atoms"]
    if not isinstance(ordered_atoms, list) or not ordered_atoms:
        raise ContractValidationError("claim-input ordered_atoms must be non-empty")
    atom_ids: list[str] = []
    for expected_ordinal, atom in enumerate(ordered_atoms):
        if not isinstance(atom, Mapping):
            raise ContractValidationError("claim-input ordered Atom must be an object")
        _assert_exact_fields(atom, _CLAIM_INPUT_ATOM_FIELDS, "claim-input ordered Atom")
        if atom["ordinal"] != expected_ordinal:
            raise ContractValidationError("claim-input Atom ordinals must be contiguous from zero")
        _assert_id(atom["atom_id"], "claim-input.atom_id")
        _assert_hash_string(atom["payload_hash"], "claim-input.payload_hash")
        acceptance_ordinal = atom["acceptance_ordinal"]
        if isinstance(acceptance_ordinal, bool) or not isinstance(acceptance_ordinal, int) or acceptance_ordinal < 1:
            raise ContractValidationError("claim-input acceptance_ordinal must be a positive integer")
        spans = atom["evidence_spans"]
        if not isinstance(spans, list) or not spans:
            raise ContractValidationError("accepted Atom must carry at least one EvidenceSpan")
        for span in spans:
            verify_evidence_span(span, canonical_text, expected_canonical_text_hash=expected_canonical_text_hash)
            if span["revision_id"] != claim_input["source_revision_id"]:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "EvidenceSpan crosses the claim source Revision")
        atom_ids.append(atom["atom_id"])
    _assert_unique(atom_ids, "claim-input Atom IDs must be unique")


def _resolve_authoritative_atom(source: AcceptedAtomSource | None, atom_id: str) -> Mapping[str, Any]:
    if source is None:
        raise ContractValidationError("claim-input sealing requires authoritative Atom records")
    candidate: Mapping[str, Any] | None
    if callable(source):
        candidate = source(atom_id)
    elif isinstance(source, Mapping):
        # A single authoritative Atom record is accepted for the one-record
        # case; otherwise the mapping is keyed by exact atom_id.
        if "atom_id" in source:
            candidate = source
        else:
            raw = source.get(atom_id)
            candidate = raw if isinstance(raw, Mapping) else None
    else:
        matches = [item for item in source if isinstance(item, Mapping) and item.get("atom_id") == atom_id]
        if len(matches) > 1:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input authority contains duplicate Atom records")
        candidate = matches[0] if matches else None
    if candidate is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input Atom is not an authoritative current Atom")
    return candidate


def _verify_claim_input_authority(
    claim_input: Mapping[str, Any],
    accepted_atoms: AcceptedAtomSource | None,
) -> None:
    source_revision_id = claim_input["source_revision_id"]
    for atom in claim_input["ordered_atoms"]:
        authority = _resolve_authoritative_atom(accepted_atoms, atom["atom_id"])
        required = ("atom_id", "payload_hash", "acceptance_ordinal", "current", "accepted")
        missing = [field for field in required if field not in authority]
        if missing:
            raise ContractValidationError(f"authoritative Atom is missing required fields: {', '.join(missing)}")
        _assert_id(authority["atom_id"], "authoritative Atom.atom_id")
        _assert_hash_string(authority["payload_hash"], "authoritative Atom.payload_hash")
        if authority["atom_id"] != atom["atom_id"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "authoritative Atom ID does not match claim-input Atom")
        if authority["current"] is not True or authority["accepted"] is not True:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input Atom is not current and accepted")
        acceptance_ordinal = authority["acceptance_ordinal"]
        if isinstance(acceptance_ordinal, bool) or not isinstance(acceptance_ordinal, int) or acceptance_ordinal < 1:
            raise ContractValidationError("authoritative Atom acceptance_ordinal must be a positive integer")
        revisions = [authority[field] for field in ("revision_id", "source_revision_id") if field in authority]
        if not revisions or any(not isinstance(revision, str) or revision != source_revision_id for revision in revisions):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "authoritative Atom belongs to another source Revision")
        if authority["payload_hash"] != atom["payload_hash"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input payload_hash drifted from the authoritative Atom")
        if acceptance_ordinal != atom["acceptance_ordinal"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input acceptance_ordinal drifted from the authoritative Atom")


def verify_claim_input(
    claim_input: Mapping[str, Any],
    *,
    expected_source_revision_id: str | None = None,
    accepted_atoms: AcceptedAtomSource | None = None,
    canonical_text: str | None = None,
    expected_canonical_text_hash: str | None = None,
) -> None:
    """Verify claim input for an authority-bearing Claim boundary."""

    verify_claim_input_structure(
        claim_input,
        expected_source_revision_id=expected_source_revision_id,
        canonical_text=canonical_text,
        expected_canonical_text_hash=expected_canonical_text_hash,
    )
    _verify_claim_input_authority(claim_input, accepted_atoms)


def build_claim_input_asset(
    source_revision_id: str,
    ordered_atoms: Sequence[Mapping[str, Any]],
    *,
    accepted_atoms: AcceptedAtomSource | None = None,
    canonical_text: str | None = None,
    expected_canonical_text_hash: str | None = None,
) -> tuple[bytes, str]:
    """Build deterministic JCS bytes and its exact Asset hash."""

    value = {
        "schema": "claim-input/v1",
        "source_revision_id": source_revision_id,
        "ordered_atoms": [copy.deepcopy(dict(atom)) for atom in ordered_atoms],
    }
    verify_claim_input(
        value,
        accepted_atoms=accepted_atoms,
        canonical_text=canonical_text,
        expected_canonical_text_hash=expected_canonical_text_hash,
    )
    raw = canonical_bytes(value)
    return raw, sha256_hex(raw)


def verify_claim_input_asset(
    raw_asset_bytes: bytes | bytearray | memoryview,
    run_snapshot: Mapping[str, Any],
    *,
    asset_id: str | None = None,
    expected_source_revision_id: str | None = None,
    accepted_atoms: AcceptedAtomSource | None = None,
    canonical_text: str | None = None,
    expected_canonical_text_hash: str | None = None,
) -> dict[str, Any]:
    """Bind claim-input bytes to the RunSnapshot parameters Asset and key."""

    if not isinstance(raw_asset_bytes, (bytes, bytearray, memoryview)):
        raise ContractValidationError("claim-input Asset must be raw bytes")
    verify_snapshot(run_snapshot)
    expected_asset_id = run_snapshot.get("parameters_asset_id")
    if not isinstance(expected_asset_id, str):
        raise ContractValidationError("RunSnapshot has no claim-input parameters Asset")
    if asset_id is not None and asset_id != expected_asset_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input Asset is not RunSnapshot parameters_asset_id")
    assets = {entry["asset_id"]: entry["sha256"] for entry in run_snapshot["asset_hashes"]}
    expected_hash = assets.get(expected_asset_id)
    if expected_hash is None:
        raise ContractValidationError("claim-input Asset hash is absent from RunSnapshot asset_hashes")
    raw = bytes(raw_asset_bytes)
    if sha256_hex(raw) != expected_hash:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "claim-input Asset bytes do not match the RunSnapshot hash")
    parsed = parse_json_bytes(raw)
    if not isinstance(parsed, Mapping) or canonical_bytes(parsed) != raw:
        raise ContractValidationError("claim-input Asset must be exact RFC 8785 JCS bytes")
    verify_claim_input(
        parsed,
        expected_source_revision_id=expected_source_revision_id,
        accepted_atoms=accepted_atoms,
        canonical_text=canonical_text,
        expected_canonical_text_hash=expected_canonical_text_hash,
    )
    return copy.deepcopy(dict(parsed))


def verify_snapshot(
    snapshot: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    interpreter_bindings: Mapping[str, Any] | None = None,
) -> None:
    assert_valid("run-snapshot/v1", snapshot)
    normalized = normalize_snapshot_for_hash(snapshot)
    _assert_hash(request_key(normalized), snapshot["request_key"], "request_key")
    _assert_hash(snapshot_hash(snapshot), snapshot["snapshot_hash"], "snapshot_hash")
    asset_ids = [entry["asset_id"] for entry in snapshot["asset_hashes"]]
    _assert_unique(asset_ids, "asset_hashes contains duplicate asset IDs")
    assets = {entry["asset_id"]: entry["sha256"] for entry in snapshot["asset_hashes"]}
    if snapshot["parameters_asset_id"] is not None and snapshot["parameters_asset_id"] not in assets:
        raise ContractValidationError("parameters_asset_id is absent from asset_hashes")
    for field, fields in (
        ("input_revisions", ("document_id", "revision_id")),
        ("plugin_releases", ("plugin_id",)),
        ("plugin_settings_revisions", ("plugin_id", "scope", "scope_id")),
        ("asset_hashes", ("asset_id",)),
        ("data_bindings", ("order",)),
        ("skill_releases", ("order",)),
    ):
        _assert_unique((_key_tuple(entry, fields) for entry in snapshot[field]), f"{field} contains duplicate identity")
    for field in ("data_bindings", "skill_releases"):
        orders = [entry["order"] for entry in snapshot[field]]
        if orders != sorted(orders):
            raise ContractValidationError(f"{field} order is not ascending")
    for binding in snapshot["data_bindings"]:
        if assets.get(binding["bundle_asset_id"]) != binding["bundle_hash"]:
            raise ContractValidationError("data binding bundle hash is not backed by asset_hashes")
    for skill in snapshot["skill_releases"]:
        parameter_id = skill["parameters_asset_id"]
        if parameter_id is not None and parameter_id not in assets:
            raise ContractValidationError("Skill parameter asset is absent from asset_hashes")
    if catalog is not None or interpreter_bindings is not None:
        if catalog is None or interpreter_bindings is None:
            raise ContractValidationError("Data bindings require both catalog and resolved interpreter bindings")
        verify_catalog(catalog)
        for binding in snapshot["data_bindings"]:
            resolved = _resolved_interpreter_pair(interpreter_bindings.get(binding["interpreter_binding_id"]))
            if resolved is None:
                raise ContractError(ErrorCode.DATA_INTERPRETER_UNAVAILABLE, f"unknown interpreter binding: {binding['interpreter_binding_id']}")
            verify_data_interpreter_binding(binding["format_id"], resolved[0], resolved[1], catalog=catalog)


def _verify_candidate_item(item: Mapping[str, Any], *, snapshot_workspace_id: str | None) -> None:
    if snapshot_workspace_id is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "candidate result verification requires a snapshot workspace",
        )
    if item.get("status") in {"failed", "skipped"}:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "failed/skipped result item cannot create or retain a Candidate",
        )
    target_value = item["target"]
    if target_value["workspace_id"] != snapshot_workspace_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate target is outside the snapshot workspace")
    target_key = (target_value["workspace_id"], target_value["entity_kind"], target_value["entity_id"])
    write_keys: list[tuple[str, str, str]] = []
    target_write_entry: Mapping[str, Any] | None = None
    for entry in item["write_set"]:
        if entry["workspace_id"] != snapshot_workspace_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate write_set crosses the snapshot workspace")
        entry_key = (entry["workspace_id"], entry["entity_kind"], entry["entity_id"])
        if entry_key in write_keys:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate write_set contains duplicate target identity")
        write_keys.append(entry_key)
        if entry_key == target_key:
            target_write_entry = entry
    if target_write_entry is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate target is not present in write_set")
    if (
        target_write_entry["revision_id"] != item["base"]["revision_id"]
        or target_write_entry["content_hash"] != item["base"]["content_hash"]
    ):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate base does not match target write_set entry")
    if item["item_kind"] == "incomplete_stream":
        if item["status"] != "partial" or target_value["entity_kind"] != "document" or item["mutation"]["mode"] != "replace":
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "incomplete_stream must be a Core-owned partial document replacement")
        if item["mutation"]["payload_schema"] != "core/document-text/v1":
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "incomplete_stream must use the Core document-text schema")
        return
    allowed = {
        "document": {"item_kind": {"document"}, "mode": {"replace", "text_patch", "append_text"}},
        "node_structure": {"item_kind": {"node_structure"}, "mode": {"structure_patch"}},
        "relation_set": {"item_kind": {"relation_set"}, "mode": {"relation_patch"}},
    }
    expected = allowed[target_value["entity_kind"]]
    if item["item_kind"] not in expected["item_kind"] or item["mutation"]["mode"] not in expected["mode"]:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate item kind/mutation does not match target")


def verify_candidate_item(item: Mapping[str, Any], *, snapshot_workspace_id: str | None) -> None:
    """Validate one Candidate item with the workspace fence applied.

    This is the public item-level seam used by the transaction staging test
    double.  It deliberately keeps the schema check next to the semantic
    checks so callers cannot stage an unverified item by bypassing a result
    Bundle.
    """

    assert_valid("candidate-item/v1", item)
    _verify_candidate_item(item, snapshot_workspace_id=snapshot_workspace_id)


def verify_parent_graph(
    items: Iterable[Mapping[str, Any]],
    *,
    known_parent_ids: set[str] | None = None,
    snapshot_workspace_id: str | None = None,
) -> None:
    item_list = list(items)
    if snapshot_workspace_id is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "candidate parent verification requires a snapshot workspace",
        )
    for item in item_list:
        # The caller normally already performed this check.  Repeating the
        # item-level fence here makes the graph helper fail closed when used
        # directly and binds every in-bundle parent to the same workspace.
        _verify_candidate_item(item, snapshot_workspace_id=snapshot_workspace_id)
    graph = {item["item_id"]: set(item["parent_candidate_ids"]) for item in item_list}
    known = set(graph) | (known_parent_ids or set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate parent cycle detected")
        if node in visited or node not in graph:
            return
        visiting.add(node)
        for parent in graph[node]:
            if parent not in known:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate parent does not exist")
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for item_id in graph:
        visit(item_id)


def _verify_chain_ref(
    ref: Mapping[str, Any],
    *,
    bundle_id: str | None = None,
    item_ids: set[str] | None = None,
    allow_stream: bool = True,
    allow_bundleless: bool = False,
) -> None:
    if (ref.get("asset_id") is None) != (ref.get("asset_hash") is None):
        raise ContractValidationError("Skill chain asset ID/hash must be all-null or all-present")
    bundle_pair = ref["result_bundle_id"] is not None and ref["result_item_id"] is not None
    stream_pair = ref["stream_id"] is not None and ref["acked_prefix_hash"] is not None
    if (ref["result_bundle_id"] is None) != (ref["result_item_id"] is None):
        raise ContractValidationError("bundle anchor must be all-null or all-present")
    if (ref["stream_id"] is None) != (ref["acked_prefix_hash"] is None):
        raise ContractValidationError("stream anchor must be all-null or all-present")
    if bundle_pair and stream_pair:
        raise ContractValidationError("Skill chain reference must have exactly one anchor profile")
    if not bundle_pair and not stream_pair and not allow_bundleless:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "bundleless Skill reference is not allowed here")
    if stream_pair and not allow_stream:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result Bundle refs must be bundle-backed")
    if bundle_pair:
        if bundle_id is not None and ref["result_bundle_id"] != bundle_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill reference points at another Bundle")
        if item_ids is not None and ref["result_item_id"] not in item_ids:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill reference points at an unknown item")


def verify_result_bundle(
    bundle: Mapping[str, Any],
    *,
    snapshot_workspace_id: str | None = None,
    snapshot_hash_value: str | None = None,
    known_parent_ids: set[str] | None = None,
    attempt_state: str | None = None,
    attempt_status: str | None = None,
) -> None:
    if attempt_state is not None and attempt_status is not None and attempt_state != attempt_status:
        raise ContractValidationError("attempt_state and attempt_status disagree")
    effective_attempt_state = attempt_state if attempt_state is not None else attempt_status
    if effective_attempt_state in {"failed", "skipped"} and (
        bundle.get("contract_id") != "diagnostic-bundle/v1" or bundle.get("bundle_type") != "diagnostic"
    ):
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "failed/skipped Attempt may only return null or diagnostic-bundle/v1",
        )
    assert_valid("result-bundle/v1", bundle)
    profile = {
        "candidate-batch/v1": ("candidate_batch", "candidate-item/v1"),
        "artifact-bundle/v1": ("artifact", "artifact-item/v1"),
        "diagnostic-bundle/v1": ("diagnostic", "diagnostic-item/v1"),
    }
    expected_type, expected_item_schema = profile[bundle["contract_id"]]
    if bundle["bundle_type"] != expected_type:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result contract and bundle type do not match")
    item_id_values = [item["item_id"] for item in bundle["items"]]
    # Check the sequence before materializing a set; doing it after set()
    # silently erased duplicate IDs in the old verifier.
    _assert_unique(item_id_values, "result bundle item IDs must be unique")
    item_ids = set(item_id_values)
    incomplete_targets: set[tuple[str, str, str]] = set()
    for item in bundle["items"]:
        if item["schema"] != expected_item_schema:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result bundle item profile does not match contract")
        if expected_item_schema == "candidate-item/v1":
            _verify_candidate_item(item, snapshot_workspace_id=snapshot_workspace_id)
            if item["item_kind"] == "incomplete_stream":
                target = item["target"]
                target_key = (target["workspace_id"], target["entity_kind"], target["entity_id"])
                if target_key in incomplete_targets:
                    raise ContractError(ErrorCode.INVALID_TRANSITION, "only one incomplete stream Candidate is allowed per target")
                incomplete_targets.add(target_key)
    if expected_item_schema == "candidate-item/v1":
        if snapshot_workspace_id is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "candidate result verification requires a snapshot workspace",
            )
        verify_parent_graph(
            bundle["items"],
            known_parent_ids=known_parent_ids,
            snapshot_workspace_id=snapshot_workspace_id,
        )
    if snapshot_hash_value is not None and bundle["input_snapshot_hash"] != snapshot_hash_value:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "bundle input snapshot does not match current snapshot")
    for ref in bundle["skill_chain_result_refs"]:
        _verify_chain_ref(ref, bundle_id=bundle["bundle_id"], item_ids=item_ids, allow_stream=False)
    statuses = [item["status"] for item in bundle["items"]]
    if bundle["partial"] and not any(status in {"partial", "failed", "skipped"} for status in statuses):
        raise ContractValidationError("partial result bundle must expose a non-complete item")
    if not bundle["partial"] and any(status != "complete" for status in statuses):
        raise ContractValidationError("complete result bundle cannot contain partial/failed/skipped items")


def verify_attempt_result(
    bundle: Mapping[str, Any] | None,
    *,
    attempt_state: str,
    snapshot_workspace_id: str | None = None,
    snapshot_hash_value: str | None = None,
    known_parent_ids: set[str] | None = None,
) -> None:
    """Enforce the Attempt/result profile before any staging side effect.

    A failed or skipped Attempt has no ordinary Candidate result: it may be
    represented only by a null Bundle or a diagnostic Bundle.  The optional
    context arguments are intentionally forwarded to the normal Bundle
    verifier so the same closed/hash/identity rules apply to both paths.
    """

    if attempt_state in {"failed", "skipped"}:
        if bundle is None:
            return
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=snapshot_workspace_id,
            snapshot_hash_value=snapshot_hash_value,
            known_parent_ids=known_parent_ids,
            attempt_state=attempt_state,
        )
        return
    if bundle is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "non-failed Attempt requires a result Bundle",
        )
    verify_result_bundle(
        bundle,
        snapshot_workspace_id=snapshot_workspace_id,
        snapshot_hash_value=snapshot_hash_value,
        known_parent_ids=known_parent_ids,
        attempt_state=attempt_state,
    )


@dataclass(frozen=True)
class CandidateStageReceipt:
    """Observable state transition returned by Candidate staging."""

    candidate_ids: tuple[str, ...]
    state: str

    @property
    def published(self) -> bool:
        return self.state == "published"

    @property
    def rolled_back(self) -> bool:
        return self.state == "rolled_back"


class CandidateStagingStore:
    """Small Host-side transaction seam for visible Candidate rows.

    ``stage`` only validates into a private transaction buffer.  ``publish``
    swaps a copied visible map in one operation, while every failed/skipped
    Attempt and every validation/commit exception closes the transaction via
    rollback without touching the externally visible map.
    """

    def __init__(
        self,
        workspace_id: str,
        visible_candidates: Iterable[Mapping[str, Any]] = (),
        *,
        known_parent_ids: Iterable[str] = (),
    ) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ContractValidationError("Candidate staging requires a non-empty workspace_id")
        self.workspace_id = workspace_id
        self._visible: dict[str, dict[str, Any]] = {}
        self._known_parent_ids = {str(value) for value in known_parent_ids}
        self._version = 0
        for candidate in visible_candidates:
            value = copy.deepcopy(dict(candidate))
            verify_candidate_item(value, snapshot_workspace_id=workspace_id)
            candidate_id = value["item_id"]
            if candidate_id in self._visible:
                raise ContractValidationError("visible Candidate IDs must be unique")
            self._visible[candidate_id] = value
        verify_parent_graph(
            self._visible.values(),
            known_parent_ids=self._known_parent_ids,
            snapshot_workspace_id=workspace_id,
        )

    def visible_candidates(self) -> tuple[dict[str, Any], ...]:
        """Return a detached snapshot; callers cannot mutate visible state."""

        return tuple(copy.deepcopy(value) for value in self._visible.values())

    def visible_candidate_ids(self) -> tuple[str, ...]:
        return tuple(self._visible)

    def transaction(self) -> "CandidateStagingTransaction":
        return CandidateStagingTransaction(self)

    def begin_transaction(self) -> "CandidateStagingTransaction":
        return self.transaction()

    def stage_and_publish(
        self,
        bundle: Mapping[str, Any] | None,
        *,
        attempt_state: str = "succeeded",
    ) -> CandidateStageReceipt:
        transaction = self.transaction()
        try:
            staged = transaction.stage(bundle, attempt_state=attempt_state)
            if staged.rolled_back:
                return staged
            return transaction.publish()
        except Exception:
            transaction.rollback()
            raise


class CandidateStagingTransaction:
    """One-shot staging buffer with explicit publish/rollback control."""

    def __init__(self, store: CandidateStagingStore) -> None:
        self._store = store
        self._base_version = store._version
        self._pending: dict[str, dict[str, Any]] = {}
        self._bundle: dict[str, Any] | None = None
        self._state = "open"

    def _ensure_open(self) -> None:
        if self._state != "open":
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Candidate staging transaction is closed")

    def stage(
        self,
        bundle: Mapping[str, Any] | None,
        *,
        attempt_state: str = "succeeded",
    ) -> CandidateStageReceipt:
        self._ensure_open()
        try:
            verify_attempt_result(
                bundle,
                attempt_state=attempt_state,
                snapshot_workspace_id=self._store.workspace_id,
                known_parent_ids=set(self._store._visible) | self._store._known_parent_ids,
            )
            if bundle is None or bundle.get("contract_id") != "candidate-batch/v1":
                return self.rollback()
            self._pending = {
                item["item_id"]: copy.deepcopy(dict(item))
                for item in bundle["items"]
            }
            self._bundle = copy.deepcopy(dict(bundle))
            return CandidateStageReceipt(tuple(self._pending), "staged")
        except Exception:
            self.rollback()
            raise

    def publish(self) -> CandidateStageReceipt:
        self._ensure_open()
        try:
            if self._bundle is None:
                return self.rollback()
            if self._store._version != self._base_version:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Candidate staging base changed before publish")
            verify_result_bundle(
                self._bundle,
                snapshot_workspace_id=self._store.workspace_id,
                known_parent_ids=set(self._store._visible) | self._store._known_parent_ids,
            )
            updated = copy.deepcopy(self._store._visible)
            for candidate_id, candidate in self._pending.items():
                previous = updated.get(candidate_id)
                if previous is not None and previous != candidate:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Candidate ID is already visible with different content")
                updated[candidate_id] = copy.deepcopy(candidate)
            self._store._visible = updated
            self._store._version += 1
            self._state = "published"
            return CandidateStageReceipt(tuple(self._pending), "published")
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> CandidateStageReceipt:
        if self._state == "published":
            raise ContractError(ErrorCode.INVALID_TRANSITION, "published Candidate staging cannot be rolled back")
        self._pending.clear()
        self._bundle = None
        self._state = "rolled_back"
        return CandidateStageReceipt((), "rolled_back")

    def __enter__(self) -> "CandidateStagingTransaction":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        if self._state == "open":
            self.rollback()
        return False


def verify_package_manifest(files: Mapping[str, bytes], manifest_bytes: bytes) -> None:
    if manifest_bytes.startswith(b"\xef\xbb\xbf"):
        raise ContractValidationError("files.sha256 must not contain a UTF-8 BOM")
    try:
        text = manifest_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractValidationError("files.sha256 must be UTF-8") from exc
    if not text or "\r" in text or not text.endswith("\n"):
        raise ContractValidationError("files.sha256 must use LF and end with a newline")
    parsed: dict[str, str] = {}
    parsed_paths: list[str] = []
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)\n", line)
        if match is None:
            raise ContractValidationError("files.sha256 line must be '<64hex><two spaces><path>\\n'")
        digest, raw_path = match.groups()
        path = normalize_relative_path(raw_path)
        if path in parsed or unicode_nfc_casefold(path) in {unicode_nfc_casefold(item) for item in parsed}:
            raise ContractValidationError("files.sha256 contains a duplicate path")
        parsed[path] = digest
        parsed_paths.append(path)
    normalized = {normalize_relative_path(path): sha256_hex(content) for path, content in files.items()}
    expected = {path: normalized[path] for path in _utf8_sort(normalized)}
    if parsed_paths != list(expected):
        raise ContractValidationError("files.sha256 paths must be sorted by normalized UTF-8 path")
    if parsed != expected:
        raise ContractValidationError("files.sha256 does not exactly describe package files")


def verify_package_files(files: Mapping[str, bytes], expected_files_sha256: bytes | None = None) -> str:
    calculated = build_files_sha256(files)
    if expected_files_sha256 is not None:
        verify_package_manifest(files, expected_files_sha256)
        if calculated != expected_files_sha256:
            raise ContractValidationError("files.sha256 bytes mismatch")
    return package_hash(files)


def verify_package_identity(
    files: Mapping[str, bytes],
    plugin_id: str,
    version: str,
    expected_package_hash: str,
    expected_release_id: str,
    *,
    expected_files_sha256: bytes | None = None,
) -> None:
    digest = verify_package_files(files, expected_files_sha256)
    _assert_hash(digest, expected_package_hash, "package_hash")
    _assert_hash(release_id(plugin_id, version, digest), expected_release_id, "release_id")


def verify_skill_identity(
    files: Mapping[str, bytes],
    skill_id: str,
    version: str,
    expected_package_hash: str,
    expected_release_id: str,
    *,
    expected_files_sha256: bytes | None = None,
) -> None:
    calculated_manifest = build_files_sha256(files)
    digest = skill_package_hash(files)
    if expected_files_sha256 is not None:
        verify_package_manifest(files, expected_files_sha256)
        if calculated_manifest != expected_files_sha256:
            raise ContractValidationError("Skill files.sha256 bytes mismatch")
    _assert_hash(digest, expected_package_hash, "skill_package_hash")
    _assert_hash(skill_release_id(skill_id, version, digest), expected_release_id, "skill_release_id")


def verify_self_hash(contract_id: str, value: Mapping[str, Any], field: str, prefix: str) -> None:
    assert_valid(contract_id, value)
    _assert_hash(hash_without_field(value, field, prefix), value[field], field)


def verify_backup(backup: Mapping[str, Any]) -> None:
    assert_valid("backup-bundle/v1", backup)
    _assert_hash(hash_without_field(backup, "bundle_hash", "plotpilot-backup/v1"), backup["bundle_hash"], "bundle_hash")
    paths = [normalize_relative_path(entry["path"]) for entry in backup["files"]]
    if paths != _utf8_sort(paths):
        raise ContractValidationError("backup files must be sorted by normalized UTF-8 path")
    _casefold_unique(paths, "backup files must be NFC/casefold-unique")
    roles = {entry["role"] for entry in backup["files"]}
    if backup["mode"] == "workspace" and roles & {"plugin_db", "package"}:
        raise ContractValidationError("workspace backup cannot contain plugin DB/package")
    if backup["mode"] == "data" and "package" in roles:
        raise ContractValidationError("data backup cannot contain packages")
    if not all(backup["verification"].values()):
        raise ContractValidationError("backup verification flags must all be true for a publishable bundle")


def verify_restore_report(report: Mapping[str, Any]) -> None:
    assert_valid("restore-report/v1", report)
    if report["source_root_id"] == report["target_root_id"]:
        raise ContractValidationError("restore staging must use a distinct target root")
    if report["state"] == "restore_ready" and report["completed_at"] is None:
        raise ContractValidationError("restore_ready requires completed_at")


def _resolved_interpreter_pair(value: Any) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        plugin_id = value.get("plugin_id")
        capability_id = value.get("capability_id")
        if isinstance(plugin_id, str) and isinstance(capability_id, str):
            return plugin_id, capability_id
    elif isinstance(value, (tuple, list)) and len(value) == 2 and all(isinstance(item, str) for item in value):
        return value[0], value[1]
    return None


def verify_plan(
    plan: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
    interpreter_bindings: Mapping[str, Any] | None = None,
) -> None:
    assert_valid("plugin-plan/v1", plan)
    # The accepted historical schema still contains the retired compare enum
    # for compatibility with the donor manifest.  The frozen PlotPilot plan
    # semantics are narrower: compare is not an executable mode; callers must
    # use separate results or an explicit synthesizer binding.
    if plan["result_mode"] not in {"separate", "synthesize"}:
        raise ContractValidationError("plugin-plan/v1 result_mode must be separate or synthesize")
    for field, id_field in (("bindings", "binding_id"), ("data_bindings", "data_binding_id")):
        _assert_unique((item[id_field] for item in plan[field]), f"{field} IDs must be unique")
        orders = [item["order"] for item in plan[field]]
        if len(orders) != len(set(orders)) or orders != sorted(orders):
            raise ContractValidationError(f"{field} order must be unique and ascending")
    synthesizer = plan["synthesizer"]
    if plan["result_mode"] == "synthesize":
        if synthesizer is None:
            raise ContractValidationError("synthesize plan requires a synthesizer")
        match = [item for item in plan["bindings"] if item["binding_id"] == synthesizer["binding_id"]]
        if len(match) != 1 or not match[0]["enabled"]:
            raise ContractValidationError("synthesizer must resolve to an enabled binding")
        if any(match[0][field] != synthesizer[field] for field in ("capability_id", "plugin_id", "release_requirement")):
            raise ContractValidationError("synthesizer identity does not match its binding")
    elif synthesizer is not None:
        raise ContractValidationError("non-synthesize plan must not declare a synthesizer")
    if catalog is not None or interpreter_bindings is not None:
        if catalog is None or interpreter_bindings is None:
            raise ContractValidationError("Data bindings require both catalog and resolved interpreter bindings")
        verify_catalog(catalog)
        for binding in plan["data_bindings"]:
            resolved = _resolved_interpreter_pair(interpreter_bindings.get(binding["interpreter_binding_id"]))
            if resolved is None:
                raise ContractError(ErrorCode.DATA_INTERPRETER_UNAVAILABLE, f"unknown interpreter binding: {binding['interpreter_binding_id']}")
            verify_data_interpreter_binding(binding["format_id"], resolved[0], resolved[1], catalog=catalog)


def verify_generation(generation: Mapping[str, Any]) -> None:
    """Verify the immutable full Generation identity, not a release ID list."""

    assert_valid("plugin-generation/v1", generation)
    _assert_unique((member["plugin_id"] for member in generation["members"]), "Generation members must contain one release per plugin")
    if generation["parent_generation_id"] == generation["generation_id"] or generation["base_generation_id"] == generation["generation_id"]:
        raise ContractValidationError("Generation cannot parent or base itself")


def verify_generation_unchanged(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    """Prove that a Generation identity is immutable once created."""

    verify_generation(previous)
    verify_generation(current)
    if previous["generation_id"] != current["generation_id"]:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Generation identity changed")
    if dict(previous) != dict(current):
        raise ContractError(ErrorCode.INVALID_TRANSITION, "Generation is immutable; current/LKG state must not be written back")


_LIFECYCLE_EDGES = {
    "selected": {"staged", "failed", "superseded"},
    "staged": {"package_published", "failed", "superseded"},
    "package_published": {"env_prepared", "failed", "superseded"},
    "env_prepared": {"shadow_prepared", "failed", "superseded"},
    "shadow_prepared": {"migrated", "failed", "superseded"},
    "migrated": {"settings_validated", "failed", "superseded"},
    "settings_validated": {"qualified", "failed", "superseded"},
    "qualified": {"pending_apply", "failed", "superseded"},
    "pending_apply": {"current_committed", "failed", "superseded"},
    "current_committed": {"lkg_pending", "rollback_armed", "failed"},
    "lkg_pending": {"lkg_promoted", "rollback_armed", "failed"},
    "lkg_promoted": set(),
    "rollback_armed": {"rolled_back", "failed"},
    "rolled_back": {"safe_mode"},
    "safe_mode": set(),
    "failed": set(),
    "superseded": set(),
}

_LIFECYCLE_IDENTITY_FIELDS = (
    "base_generation_id",
    "base_lkg_generation_id",
    "target_generation_id",
    "target_settings_revision_ids",
)
_LIFECYCLE_QUALIFIED_STATES = {
    "qualified",
    "pending_apply",
    "current_committed",
    "lkg_pending",
    "lkg_promoted",
    "rollback_armed",
    "rolled_back",
    "safe_mode",
}
_LIFECYCLE_SHADOW_STATES = {
    "shadow_prepared",
    "migrated",
    "settings_validated",
    "qualified",
    "pending_apply",
    "current_committed",
    "lkg_pending",
    "lkg_promoted",
    "rollback_armed",
    "rolled_back",
    "safe_mode",
}
_LIFECYCLE_ROLLBACK_STATES = {"rollback_armed", "rolled_back", "safe_mode"}
_PACKAGE_STORE_ORDER = {"absent": 0, "staged": 1, "published": 2, "orphan": 3}


def _lifecycle_settings_identity(transition: Mapping[str, Any]) -> bytes:
    settings = transition["target_settings_revision_ids"]
    _assert_unique((item["plugin_id"] for item in settings), "target settings must contain one revision per plugin")
    normalized = sorted(settings, key=lambda item: (item["plugin_id"], item["settings_revision_id"]))
    return canonical_bytes(normalized)


def _lifecycle_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError(f"{label} is not a valid UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{label} must be timezone-aware UTC")
    return parsed


def _verify_lifecycle_state_shape(transition: Mapping[str, Any]) -> None:
    state = transition["state"]
    target = transition["target_generation_id"]
    if state in {"current_committed", "lkg_pending", "lkg_promoted", "rollback_armed", "rolled_back", "safe_mode"} and target is None:
        raise ContractValidationError(f"{state} requires target_generation_id")
    if state in _LIFECYCLE_QUALIFIED_STATES and transition["qualification_id"] is None:
        raise ContractValidationError(f"{state} requires qualification_id")
    if state in _LIFECYCLE_SHADOW_STATES and transition["shadow_data_generation_id"] is None:
        raise ContractValidationError(f"{state} requires shadow_data_generation_id")
    if state == "failed" and transition["failure_code"] is None:
        raise ContractValidationError("failed lifecycle transition requires failure_code")
    if state != "failed" and transition["failure_code"] is not None:
        raise ContractValidationError("non-failed lifecycle transition cannot carry failure_code")
    if state == "rollback_armed":
        if transition["base_lkg_generation_id"] is None:
            raise ContractValidationError("rollback_armed requires an exact base LKG Generation")
        if transition["rollback_token"] is None or transition["rollback_attempt"] != 0:
            raise ContractValidationError("rollback_armed requires a fresh rollback token and zero attempts")
    if state in {"rolled_back", "safe_mode"}:
        if transition["base_lkg_generation_id"] is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, f"{state} requires the exact base LKG Generation")
        if transition["rollback_attempt"] != 1 or transition["rollback_token"] is None:
            raise ContractValidationError(f"{state} requires one rollback attempt and a rollback token")
        if transition["package_store_status"] != "published":
            raise ContractError(ErrorCode.INVALID_TRANSITION, f"{state} requires the LKG package to remain published")
    if state == "lkg_promoted" and transition["rollback_attempt"] != 0:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "LKG promotion is not a Generation rollback")
    if state not in _LIFECYCLE_ROLLBACK_STATES and transition["rollback_token"] is not None:
        raise ContractValidationError("rollback_token is only valid for the rollback action")


def _verify_lifecycle_identity(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    for field in _LIFECYCLE_IDENTITY_FIELDS:
        if field == "target_settings_revision_ids":
            if _lifecycle_settings_identity(previous) != _lifecycle_settings_identity(current):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "install target settings identity changed")
        elif field == "target_generation_id" and previous[field] != current[field]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "install target_generation_id changed")
        elif previous[field] != current[field]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, f"install operation {field} changed")

    if previous["qualification_id"] != current["qualification_id"]:
        if not (
            previous["qualification_id"] is None
            and current["qualification_id"] is not None
            and current["state"] in _LIFECYCLE_QUALIFIED_STATES
        ):
            raise ContractError(ErrorCode.INVALID_TRANSITION, "qualification identity changed")
    if previous["shadow_data_generation_id"] != current["shadow_data_generation_id"]:
        if not (
            previous["shadow_data_generation_id"] is None
            and current["shadow_data_generation_id"] is not None
            and current["state"] in _LIFECYCLE_SHADOW_STATES
        ):
            raise ContractError(ErrorCode.INVALID_TRANSITION, "shadow data Generation identity changed")
    if previous["rollback_token"] != current["rollback_token"]:
        if not (
            previous["rollback_token"] is None
            and current["rollback_token"] is not None
            and current["state"] in _LIFECYCLE_ROLLBACK_STATES
        ):
            raise ContractError(ErrorCode.INVALID_TRANSITION, "rollback token identity changed")


def _verify_lifecycle_progression(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    previous_state = previous["state"]
    state = current["state"]
    if state == previous_state and state in {"lkg_promoted", "rolled_back", "safe_mode", "failed", "superseded"}:
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"terminal lifecycle state {state} cannot be repeated")
    if state != previous_state and state not in _LIFECYCLE_EDGES[previous_state]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"illegal lifecycle transition {previous_state} -> {state}")
    if current["rollback_attempt"] < previous["rollback_attempt"]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "rollback attempt moved backwards")
    if previous["rollback_attempt"] == 1 and current["rollback_attempt"] != 1:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "completed rollback attempt cannot be cleared")
    if _PACKAGE_STORE_ORDER[current["package_store_status"]] < _PACKAGE_STORE_ORDER[previous["package_store_status"]]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "package-store status moved backwards")
    if _lifecycle_time(current["created_at"], "created_at") != _lifecycle_time(previous["created_at"], "created_at"):
        raise ContractError(ErrorCode.INVALID_TRANSITION, "install operation created_at changed")
    if _lifecycle_time(current["updated_at"], "updated_at") < _lifecycle_time(previous["updated_at"], "updated_at"):
        raise ContractError(ErrorCode.INVALID_TRANSITION, "lifecycle updated_at moved backwards")


def verify_lifecycle_transition(
    transition: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
) -> None:
    """Verify lifecycle ordering and the one-shot LKG rollback contract."""

    assert_valid("plugin-lifecycle-transition/v1", transition)
    _verify_lifecycle_state_shape(transition)
    if previous is not None:
        assert_valid("plugin-lifecycle-transition/v1", previous)
        _verify_lifecycle_state_shape(previous)
        if previous["install_operation_id"] != transition["install_operation_id"]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "lifecycle transition changed install operation identity")
        _verify_lifecycle_identity(previous, transition)
        _verify_lifecycle_progression(previous, transition)


def verify_release_retirement(
    retirement: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
) -> None:
    """Verify installed -> retiring -> retired and package deletion fencing."""

    assert_valid("release-retirement/v1", retirement)
    state = retirement["state"]
    if state == "installed" and (retirement["started_at"] is not None or retirement["completed_at"] is not None):
        raise ContractValidationError("installed release cannot have retirement timestamps")
    if state == "retiring" and (retirement["started_at"] is None or retirement["completed_at"] is not None or not retirement["package_present"]):
        raise ContractValidationError("retiring release must retain its package until pins drain")
    if state == "retired" and (retirement["started_at"] is None or retirement["completed_at"] is None or retirement["package_present"]):
        raise ContractValidationError("retired release must have completed retirement and no package")
    if previous is not None:
        assert_valid("release-retirement/v1", previous)
        if previous["release_id"] != retirement["release_id"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "retirement release identity changed")
        allowed = {"installed": {"retiring"}, "retiring": {"retired"}, "retired": set()}[previous["state"]]
        if retirement["state"] != previous["state"] and retirement["state"] not in allowed:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "release retirement state moved backwards")
        if retirement["retire_epoch"] < previous["retire_epoch"]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "retire epoch moved backwards")
        if previous["state"] == "installed" and retirement["state"] == "retiring" and retirement["retire_epoch"] != previous["retire_epoch"] + 1:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "retiring must increment retire epoch exactly once")
        if previous["state"] == retirement["state"] and retirement["retire_epoch"] != previous["retire_epoch"]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "retire epoch changed without a state transition")


def verify_release_pin(pin: Mapping[str, Any], *, retirement: Mapping[str, Any] | None = None) -> None:
    """Verify active/released pin shape and the retiring pin barrier."""

    assert_valid("release-pin/v1", pin)
    if retirement is not None:
        verify_release_retirement(retirement)
        if retirement["release_id"] != pin["release_id"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "pin targets another release")
        if retirement["state"] == "retired" and pin["released_at"] is None:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "retired release cannot retain an active pin")
        if retirement["state"] == "retiring" and pin["released_at"] is None and pin["retire_epoch"] >= retirement["retire_epoch"]:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "new executable pin cannot race a retiring release")


def verify_core_snapshot(snapshot: Mapping[str, Any]) -> None:
    assert_valid("core-snapshot/v1", snapshot)
    if snapshot["coverage_complete"] is not True:
        raise ContractValidationError("core snapshot must be coverage-complete")
    event_types = snapshot["subscription_scope"]["event_types"]
    if event_types != sorted(set(event_types)):
        raise ContractValidationError("core snapshot event_types must be a sorted set")
    keys = [_key_tuple(item, ("aggregate_type", "aggregate_id")) for item in snapshot["covered_aggregates"]]
    if keys != sorted(set(keys)):
        raise ContractValidationError("covered aggregates must be sorted and unique")
    _assert_hash(hash_without_field(snapshot, "snapshot_hash", "core-snapshot/v1"), snapshot["snapshot_hash"], "snapshot_hash")


def verify_job_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Verify the self-hash and the monotonic identity fields of a Job view."""
    assert_valid("job-snapshot/v1", snapshot)
    _assert_unique((step["step_id"] for step in snapshot["steps"]), "job snapshot step IDs must be unique")
    _assert_unique((attempt["attempt_id"] for attempt in snapshot["attempts"]), "job snapshot attempt IDs must be unique")
    _assert_hash(hash_without_field(snapshot, "snapshot_hash", "job-snapshot/v1"), snapshot["snapshot_hash"], "snapshot_hash")


def verify_settings_validation_receipt(receipt: Mapping[str, Any]) -> None:
    """Verify the Core-materialized settings validation receipt digest."""
    assert_valid("settings-validation-receipt/v1", receipt)
    _assert_hash(hash_without_field(receipt, "receipt_hash", "settings-validation-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_capability_descriptor(
    descriptor: Mapping[str, Any],
    *,
    expected_capability_id: str | None = None,
    allowed_capability_ids: set[str] | None = None,
    expected_provider: Mapping[str, Any] | None = None,
) -> None:
    """Validate a descriptor and bind it to the capability being described."""
    assert_valid("capability-provider/v1", descriptor)
    if expected_capability_id is not None and descriptor["capability_id"] != expected_capability_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor ID does not match the request")
    if allowed_capability_ids is not None and descriptor["capability_id"] not in allowed_capability_ids:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor references an unknown capability")
    if expected_provider is not None and dict(descriptor["provider"]) != dict(expected_provider):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor provider does not match the installed release")
    _assert_unique(descriptor["supports"], "capability descriptor supports must be unique")
    _assert_unique(descriptor["accepted_data_formats"], "capability descriptor data formats must be unique")


def verify_provenance_receipt(receipt: Mapping[str, Any]) -> None:
    """Verify provenance's nullable Bundle pair, chain refs and self-hash."""
    assert_valid("provenance-receipt/v1", receipt)
    if (receipt["bundle_id"] is None) != (receipt["bundle_hash"] is None):
        raise ContractValidationError("provenance Bundle ID/hash must be all-null or all-present")
    _assert_unique((item["item_id"] for item in receipt["staged_items"]), "provenance staged item IDs must be unique")
    for ref in receipt["skill_chain_result_refs"]:
        _verify_chain_ref(ref, bundle_id=receipt["bundle_id"], allow_bundleless=True)
    _assert_hash(hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_sse_recovery(value: Mapping[str, Any]) -> None:
    assert_valid("sse-recovery/v1", value)
    if value["gap"] != value["snapshot_required"]:
        raise ContractValidationError("SSE snapshot_required must equal gap")
    if value["stream_kind"] == "core_event" and value["aggregate_id"] is not None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "core event recovery cannot carry an aggregate job ID")
    if value["requested_after_seq"] > value["durable_high_water_seq"]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "SSE cursor is ahead of the durable high-water mark")
    present = [value[field] is not None for field in ("snapshot_schema", "snapshot_revision", "snapshot_asset_id", "snapshot_hash")]
    if any(present) != value["gap"] or (present and len(set(present)) != 1):
        raise ContractValidationError("SSE snapshot fields must be all-null or all-present with a gap")
    if value["gap"] and value["snapshot_schema"] != ("core-snapshot/v1" if value["stream_kind"] == "core_event" else "job-snapshot/v1"):
        raise ContractValidationError("SSE snapshot schema does not match stream kind")


def verify_checkpoint(checkpoint: Mapping[str, Any], *, expected_snapshot_hash: str | None = None, previous_seq: int | None = None) -> None:
    assert_valid("checkpoint/v1", checkpoint)
    if expected_snapshot_hash is not None and checkpoint["run_snapshot_hash"] != expected_snapshot_hash:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint belongs to another RunSnapshot")
    if previous_seq is not None and checkpoint["checkpoint_seq"] <= previous_seq:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint sequence moved backwards")
    _assert_hash(hash_without_field(checkpoint, "checkpoint_hash", "checkpoint/v1"), checkpoint["checkpoint_hash"], "checkpoint_hash")


def verify_stream_prefix(prefix: Mapping[str, Any], *, previous: Mapping[str, Any] | None = None, content: bytes | None = None) -> None:
    assert_valid("stream-prefix/v1", prefix)
    if previous is not None:
        if prefix["stream_id"] != previous["stream_id"] or prefix["attempt_id"] != previous["attempt_id"] or prefix["lease_epoch"] != previous["lease_epoch"]:
            raise ContractError(ErrorCode.STALE_LEASE, "stream prefix binding changed")
        if prefix["prefix_seq"] <= previous["prefix_seq"] or prefix["byte_length"] < previous["byte_length"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "stream prefix moved backwards")
    if content is not None:
        if len(content) != prefix["byte_length"] or sha256_hex(content) != prefix["prefix_hash"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "stream prefix asset hash/length mismatch")


def verify_skill_receipt(receipt: Mapping[str, Any]) -> None:
    assert_valid("skill-run-receipt/v1", receipt)
    _verify_chain_ref(
        {
            "result_bundle_id": receipt["result_bundle_id"],
            "result_item_id": receipt["result_item_id"],
            "stream_id": receipt["stream_id"],
            "acked_prefix_hash": receipt["acked_prefix_hash"],
        },
        allow_bundleless=True,
    )
    if receipt["result_bundle_id"] is None and receipt["stream_id"] is None and receipt["step_state"] not in {"failed", "skipped"}:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "only a failed/skipped Skill receipt may be bundleless")
    if not receipt["frozen"]:
        raise ContractValidationError("Skill receipt must be frozen before chain aggregation")
    if receipt["step_state"] == "skipped" and (receipt["participated"] or receipt["verified_patch"]):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "skipped Skill step cannot be participated or verified")
    if receipt["model_claimed"] and receipt["claim_evidence_asset_id"] is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "model_claimed requires claim evidence")
    if receipt["verified_patch"] and not any(patch["verified"] for patch in receipt["patches"]):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "verified_patch requires a verified patch")
    for patch in receipt["patches"]:
        if patch["end_codepoint"] < patch["start_codepoint"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill patch range is inverted")
    if (receipt["output_asset_id"] is None) != (receipt["output_hash"] is None):
        raise ContractValidationError("Skill output Asset ID/hash must be all-null or all-present")
    _assert_hash(hash_without_field(receipt, "receipt_hash", "skill-run-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_skill_chain(chain: Mapping[str, Any], receipts: Iterable[Mapping[str, Any]]) -> None:
    assert_valid("skill-chain-result/v1", chain)
    receipt_list = sorted(receipts, key=lambda item: item["chain_index"])
    _assert_unique((receipt["receipt_id"] for receipt in receipt_list), "Skill chain receipt IDs must be unique")
    _assert_unique((receipt["chain_index"] for receipt in receipt_list), "Skill chain receipt indexes must be unique")
    if len(receipt_list) != len(chain["receipt_ids"]) or [r["receipt_id"] for r in receipt_list] != chain["receipt_ids"]:
        raise ContractValidationError("Skill chain receipt IDs/indexes are not continuous")
    chain_ref = {
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }
    _verify_chain_ref(chain_ref, allow_bundleless=True)
    if chain["result_bundle_id"] is None and chain["stream_id"] is None and chain["chain_status"] not in {"failed", "cancelled"}:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "only a failed/cancelled Skill chain may be bundleless")
    for index, receipt in enumerate(receipt_list):
        verify_skill_receipt(receipt)
        if receipt["chain_index"] != index or receipt["chain_id"] != chain["chain_id"] or receipt["run_snapshot_hash"] != chain["run_snapshot_hash"]:
            raise ContractValidationError("Skill chain receipt index/identity mismatch")
        receipt_ref = {key: receipt[key] for key in ("result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash")}
        if receipt_ref != chain_ref:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill receipt anchor does not match its chain")
    if chain["receipt_hashes"] != [r["receipt_hash"] for r in receipt_list]:
        raise ContractValidationError("Skill chain receipt hashes do not match")
    if (chain["final_output_asset_id"] is None) != (chain["final_output_hash"] is None):
        raise ContractValidationError("final output asset/hash must be all-null or all-present")
    final_hash = chain["final_output_hash"] or "-"
    expected = sha256_hex(b"skill-chain/v1\n" + b"\n".join(r["receipt_hash"].encode("ascii") for r in receipt_list) + f"\n{final_hash}\n".encode("ascii"))
    _assert_hash(expected, chain["chain_hash"], "chain_hash")


def verify_compatibility(value: Mapping[str, Any]) -> None:
    if dict(value) != {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"}:
        raise ContractValidationError("compatibility does not match the v1 canonical matrix")
    assert_valid("compatibility-v1", value)


def _assert_exact_fields(value: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractValidationError(f"{label} fields are not bound to the method schema: missing={missing}, extra={extra}")


def validate_rpc_request(request: Mapping[str, Any], *, expected_lease_epoch: int | None = None) -> None:
    is_heartbeat = request.get("method") == "runtime.heartbeat" and "id" not in request
    assert_valid("rpc-notification/v1" if is_heartbeat else "rpc-request/v1", request)
    if is_heartbeat:
        if "id" in request:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "heartbeat must not carry an id")
        heartbeat_definition = METHOD_MATRIX["methods"]["runtime.heartbeat"]
        _assert_exact_fields(request["params"], heartbeat_definition["params"]["fields"], "runtime.heartbeat params")
        if expected_lease_epoch is not None and request["meta"]["lease_epoch"] != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "heartbeat lease epoch is stale")
        return
    method = request["method"]
    definition = METHOD_MATRIX["methods"].get(method)
    if definition is None or method == "runtime.heartbeat":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown or notification-only RPC method")
    context = request["meta"]["context"]
    if context not in definition["meta_profile"].split("/"):
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"{method} cannot use {context} meta profile")
    _assert_exact_fields(request["params"], definition["params"]["fields"], f"{method} params")
    if context in {"install", "attempt"} and expected_lease_epoch is not None:
        actual = request["meta"]["install_lease_epoch"] if context == "install" else request["meta"]["lease_epoch"]
        if actual != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "RPC lease epoch is stale")
    operation_methods = {
        "host.asset.create/v1",
        "host.model.invoke/v1",
        "host.capability.invoke/v1",
        "host.capability.cancel/v1",
        "host.candidate.stage/v1",
        "host.checkpoint.commit/v1",
        "host.stream.commit/v1",
        "host.job.event/v1",
        "host.job.await_user/v1",
        "host.job.complete/v1",
        "host.migration.lease.renew/v1",
        "host.migration.lease.release/v1",
    }
    if method in operation_methods and "operation_key" not in request["params"]:
        raise ContractValidationError(f"{method} requires operation_key")
    if method == "runtime.health":
        params = request["params"]
        lease_fields = ("db_lease_id", "db_lease_epoch", "owner_instance_id")
        if context == "install" and not all(params[key] is not None for key in lease_fields):
            raise ContractError(ErrorCode.STALE_LEASE, "install health requires all shadow DB lease fields")
        if context == "control" and any(params[key] is not None for key in lease_fields):
            raise ContractError(ErrorCode.STALE_LEASE, "control health cannot carry shadow DB lease fields")


def validate_rpc_result(
    method: str,
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
) -> None:
    """Validate a result against the exact method branch, not just any RPC result.

    The generated success schema is a closed union.  Its result branches do
    not carry the request method, so the matrix field binding below is the
    second-stage check that prevents a valid result for another method from
    being accepted here.
    """
    definition = METHOD_MATRIX["methods"].get(method)
    if definition is None or method == "runtime.heartbeat":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown or notification-only RPC method")
    if request is not None and request.get("method") != method:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC result method does not match its request")
    _assert_exact_fields(result, definition["result"]["fields"], f"{method} result")
    response = {
        "jsonrpc": "2.0",
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "result": dict(result),
    }
    assert_valid("rpc-success/v1", response)

    params = request.get("params", {}) if request is not None else {}
    if method == "capability.describe":
        verify_capability_descriptor(result["descriptor"], expected_capability_id=params.get("capability_id"))
    elif method == "runtime.handshake":
        if result["plugin_protocol"] != "1":
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake attempted a protocol downgrade or upgrade")
        if request is not None and result["release_id"] != request["meta"]["plugin_release_id"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake release does not match the request meta")
    elif method == "host.capability.invoke/v1":
        expected_contract = params.get("expected_result_contract")
        if expected_contract is not None and result["child_result_contract"] != expected_contract:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child result contract does not match the binding request")
    elif method == "job.pause" and result["accepted"] and result["checkpoint_asset_id"] is None:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "accepted job.pause must return a checkpoint Asset")


def validate_rpc_response(
    response: Mapping[str, Any],
    method: str | None = None,
    *,
    request: Mapping[str, Any] | None = None,
) -> None:
    if request is not None:
        request_method = request.get("method")
        if method is None:
            method = request_method
        elif method != request_method:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC response method does not match its request")
    if "error" in response:
        assert_valid("rpc-error-v1", response)
        if response["error"]["code"] not in EXPECTED_ERROR_CODES:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC error code is not in the v1 registry")
    else:
        assert_valid("rpc-success-v1", response)
        if method is not None:
            validate_rpc_result(method, response["result"], request=request)


def verify_contract_inventory() -> None:
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    if tuple(METHOD_MATRIX["worker_methods"]) != EXPECTED_WORKER_METHODS or tuple(WORKER_METHODS) != EXPECTED_WORKER_METHODS:
        raise ContractValidationError("RPC worker method set/order differs from §20.1")
    if tuple(METHOD_MATRIX["host_methods"]) != EXPECTED_HOST_METHODS or tuple(HOST_METHODS) != EXPECTED_HOST_METHODS:
        raise ContractValidationError("RPC host method set/order differs from §20.2")
    if set(METHOD_MATRIX["methods"]) != set(EXPECTED_WORKER_METHODS + EXPECTED_HOST_METHODS):
        raise ContractValidationError("RPC matrix method set differs from §20.4")
    if {int(key): value for key, value in METHOD_MATRIX["error_codes"].items()} != EXPECTED_ERROR_CODES:
        raise ContractValidationError("RPC error code registry differs from §20.5")


def verify_history_bytes(raw: bytes, expected: bytes) -> None:
    if raw != expected:
        raise ContractValidationError("v1 history reader changed raw Asset bytes")


def load_strict_json(path: Path) -> Any:
    return parse_json_bytes(path.read_bytes())
