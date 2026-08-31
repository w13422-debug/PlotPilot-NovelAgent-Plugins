"""Closed narrative business contracts with source/evidence closure.

The business payloads in this module deliberately remain independent from the
public RPC contracts. Runtime entry points pass canonical source text and
nodes whenever an externally supplied unit is accepted; the optional relaxed
mode is retained only for decoding already materialised payload Assets.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _sdk_canonical_bytes
except ImportError:  # pragma: no cover
    _sdk_canonical_bytes = None

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
UNIT_KINDS = frozenset({"setup", "beat", "scene", "turn", "conflict", "reversal", "climax", "resolution", "foreshadow", "payoff", "other"})
PLAN_LEVELS = ("book", "volume", "chapter", "plot_unit")
LEVEL_INDEX = {value: index for index, value in enumerate(PLAN_LEVELS)}


class NarrativeContractError(ValueError):
    """A closed narrative contract or authority binding is invalid."""


class EvidenceSpanError(NarrativeContractError):
    """EvidenceSpan is not bound to exact Unicode scalar text."""


def canonical_json_bytes(value: Any) -> bytes:
    if _sdk_canonical_bytes is not None:
        return bytes(_sdk_canonical_bytes(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def hash_json(schema: str, value: Any) -> str:
    return hashlib.sha256(schema.encode("ascii") + b"\n" + canonical_json_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(_text(value, "text", empty=True).encode("utf-8")).hexdigest()


def _closed(value: Any, fields: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NarrativeContractError(f"{label} must be an object")
    result = dict(value)
    if set(result) != set(fields):
        raise NarrativeContractError(f"{label} fields are not closed: missing={sorted(set(fields)-set(result))}, extra={sorted(set(result)-set(fields))}")
    return result


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise NarrativeContractError(f"{label} must be a {'string' if empty else 'non-empty string'}")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise NarrativeContractError(f"{label} contains a surrogate") from exc
    return value


def _id(value: Any, label: str) -> str:
    value = _text(value, label)
    if ID_RE.fullmatch(value) is None:
        raise NarrativeContractError(f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise NarrativeContractError(f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NarrativeContractError(f"{label} must be integer >= {minimum}")
    return value


EVIDENCE_FIELDS = frozenset({"schema", "workspace_id", "document_id", "revision_id", "node_id", "start_codepoint", "end_codepoint", "quote", "quote_hash", "canonical_text_hash"})
EVIDENCE_WITH_ID_FIELDS = EVIDENCE_FIELDS | {"evidence_span_id"}
NODE_FIELDS = frozenset({"node_id", "start_codepoint", "end_codepoint"})


def validate_nodes(nodes: Any, text_length: int) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or not nodes:
        raise NarrativeContractError("nodes must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(nodes):
        node = _closed(raw, NODE_FIELDS, f"nodes[{index}]")
        node_id = _id(node["node_id"], f"nodes[{index}].node_id")
        start = _integer(node["start_codepoint"], f"nodes[{index}].start_codepoint")
        end = _integer(node["end_codepoint"], f"nodes[{index}].end_codepoint", 1)
        if node_id in seen or start >= end or end > text_length:
            raise NarrativeContractError(f"nodes[{index}] has invalid identity/bounds")
        seen.add(node_id)
        result.append({"node_id": node_id, "start_codepoint": start, "end_codepoint": end})
    return result


def build_evidence_span(*, workspace_id: str, document_id: str, revision_id: str, node_id: str, start_codepoint: int, end_codepoint: int, canonical_text: str) -> dict[str, Any]:
    text = _text(canonical_text, "canonical_text", empty=True)
    start = _integer(start_codepoint, "start_codepoint")
    end = _integer(end_codepoint, "end_codepoint", 1)
    if start >= end or end > len(text):
        raise EvidenceSpanError("EvidenceSpan bounds are outside canonical Revision")
    quote = text[start:end]
    return {"schema": "evidence-span/v1", "workspace_id": _id(workspace_id, "workspace_id"), "document_id": _id(document_id, "document_id"), "revision_id": _id(revision_id, "revision_id"), "node_id": _id(node_id, "node_id"), "start_codepoint": start, "end_codepoint": end, "quote": quote, "quote_hash": sha256_text(quote), "canonical_text_hash": sha256_text(text)}


def validate_evidence_span(raw: Any, canonical_text: str, nodes: Sequence[Mapping[str, Any]], *, workspace_id: str, document_id: str, revision_id: str, canonical_text_hash: str) -> dict[str, Any]:
    span = _closed(raw, EVIDENCE_FIELDS, "evidence_span")
    if span["schema"] != "evidence-span/v1":
        raise EvidenceSpanError("EvidenceSpan schema is invalid")
    node_by_id = {node["node_id"]: node for node in nodes}
    node = node_by_id.get(span["node_id"])
    if node is None:
        raise EvidenceSpanError("EvidenceSpan node is absent")
    expected = build_evidence_span(workspace_id=workspace_id, document_id=document_id, revision_id=revision_id, node_id=span["node_id"], start_codepoint=span["start_codepoint"], end_codepoint=span["end_codepoint"], canonical_text=canonical_text)
    if span != expected or span["canonical_text_hash"] != canonical_text_hash:
        raise EvidenceSpanError("EvidenceSpan quote/hash/Revision binding changed")
    if span["start_codepoint"] < node["start_codepoint"] or span["end_codepoint"] > node["end_codepoint"]:
        raise EvidenceSpanError("EvidenceSpan escapes its node")
    return expected


ATTR_FIELDS = frozenset({"attribution_id", "source_type", "workspace_id", "source_id", "revision_or_hash", "evidence_span_ids", "child_receipt_id"})


def validate_source_attribution(raw: Any, known_spans: set[str], *, label: str = "source_attribution") -> dict[str, Any]:
    value = _closed(raw, ATTR_FIELDS, label)
    result = {"attribution_id": _id(value["attribution_id"], f"{label}.attribution_id"), "source_type": _text(value["source_type"], f"{label}.source_type"), "workspace_id": None if value["workspace_id"] is None else _id(value["workspace_id"], f"{label}.workspace_id"), "source_id": _id(value["source_id"], f"{label}.source_id"), "revision_or_hash": _text(value["revision_or_hash"], f"{label}.revision_or_hash"), "evidence_span_ids": value["evidence_span_ids"], "child_receipt_id": None if value["child_receipt_id"] is None else _id(value["child_receipt_id"], f"{label}.child_receipt_id")}
    refs = result["evidence_span_ids"]
    if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)) or any(_id(item, f"{label}.evidence_span_ids") not in known_spans for item in refs):
        raise NarrativeContractError(f"{label} must close over non-empty known EvidenceSpan IDs")
    return result


UNIT_FIELDS = frozenset({"schema", "unit_id", "unit_kind", "title", "summary", "order", "evidence_spans", "source_attributions", "relations", "provenance"})
REL_FIELDS = frozenset({"relation_type", "target_unit_id"})
PROVENANCE_FIELDS = frozenset({"source_mode", "model_receipt_id"})


def _normalise_span_without_source(span: Any, label: str) -> dict[str, Any]:
    value = _closed(span, EVIDENCE_WITH_ID_FIELDS, label)
    if value["schema"] != "evidence-span/v1":
        raise NarrativeContractError("EvidenceSpan schema invalid")
    for field in ("workspace_id", "document_id", "revision_id", "node_id"):
        _id(value[field], f"{label}.{field}")
    start = _integer(value["start_codepoint"], f"{label}.start_codepoint")
    end = _integer(value["end_codepoint"], f"{label}.end_codepoint", 1)
    quote = _text(value["quote"], f"{label}.quote", empty=True)
    if start >= end or end - start != len(quote):
        raise EvidenceSpanError(f"{label} scalar bounds/quote length mismatch")
    if sha256_text(quote) != _hash(value["quote_hash"], f"{label}.quote_hash"):
        raise EvidenceSpanError(f"{label} quote hash mismatch")
    _hash(value["canonical_text_hash"], f"{label}.canonical_text_hash")
    return {"evidence_span_id": _id(value["evidence_span_id"], f"{label}.evidence_span_id"), **{field: deepcopy(value[field]) for field in EVIDENCE_FIELDS}}


def validate_narrative_unit(raw: Any, *, canonical_text: str | None = None, nodes: Sequence[Mapping[str, Any]] | None = None, workspace_id: str | None = None, document_id: str | None = None, revision_id: str | None = None, canonical_text_hash: str | None = None, require_source: bool = False) -> dict[str, Any]:
    value = _closed(raw, UNIT_FIELDS, "narrative_unit")
    if value["schema"] != "narrative-unit/v1" or value["unit_kind"] not in UNIT_KINDS:
        raise NarrativeContractError("narrative unit schema/kind is invalid")
    spans = value["evidence_spans"]
    if not isinstance(spans, list) or not spans:
        raise NarrativeContractError("narrative unit requires EvidenceSpan")
    if canonical_text is None and require_source:
        raise EvidenceSpanError("external NarrativeUnit requires canonical Revision context")
    span_ids: set[str] = set()
    normalized_spans: list[dict[str, Any]] = []
    for index, span in enumerate(spans):
        if not isinstance(span, Mapping):
            raise NarrativeContractError(f"evidence_spans[{index}] must be an object")
        span_id = _id(span.get("evidence_span_id"), "evidence_span_id")
        if canonical_text is not None:
            if nodes is None or not workspace_id or not document_id or not revision_id or not canonical_text_hash:
                raise EvidenceSpanError("canonical Revision context is incomplete")
            normalized = validate_evidence_span({key: span[key] for key in EVIDENCE_FIELDS}, canonical_text, nodes, workspace_id=workspace_id, document_id=document_id, revision_id=revision_id, canonical_text_hash=canonical_text_hash)
            normalized = {"evidence_span_id": span_id, **normalized}
        else:
            normalized = _normalise_span_without_source(span, f"evidence_spans[{index}]")
        if span_id in span_ids:
            raise NarrativeContractError("duplicate EvidenceSpan identity")
        span_ids.add(span_id)
        normalized_spans.append(deepcopy(normalized))
    attrs = value["source_attributions"]
    if not isinstance(attrs, list) or not attrs:
        raise NarrativeContractError("narrative unit requires per-source attribution")
    normalized_attrs = [validate_source_attribution(item, span_ids, label=f"source_attributions[{i}]") for i, item in enumerate(attrs)]
    # When a Unit is accepted against a live canonical Revision, attribution
    # identity is part of the same source closure as its EvidenceSpan.  A
    # caller must not be able to attach an exact quote from ``doc-1/rev-1``
    # while attributing it to another document or revision.  The relaxed
    # materialised-Asset path intentionally leaves this check to the caller
    # that owns the source context (the worker always uses ``require_source``).
    if canonical_text is not None:
        assert workspace_id is not None and document_id is not None and revision_id is not None
        for index, attribution in enumerate(normalized_attrs):
            if (attribution["workspace_id"] != workspace_id
                    or attribution["source_id"] != document_id
                    or attribution["revision_or_hash"] != revision_id):
                raise EvidenceSpanError(
                    f"source_attributions[{index}] is not bound to the canonical Revision"
                )
    attr_ids = [item["attribution_id"] for item in normalized_attrs]
    if len(attr_ids) != len(set(attr_ids)):
        raise NarrativeContractError("duplicate source attribution")
    relations = value["relations"]
    if not isinstance(relations, list):
        raise NarrativeContractError("relations must be an array")
    normalized_relations: list[dict[str, str]] = []
    for i, raw_relation in enumerate(relations):
        relation = _closed(raw_relation, REL_FIELDS, f"relations[{i}]")
        normalized_relations.append({"relation_type": _text(relation["relation_type"], "relation_type"), "target_unit_id": _id(relation["target_unit_id"], "target_unit_id")})
    provenance = _closed(value["provenance"], PROVENANCE_FIELDS, "provenance")
    if provenance["source_mode"] not in {"model", "manual", "broker"}:
        raise NarrativeContractError("provenance source_mode invalid")
    if provenance["source_mode"] == "model" and provenance["model_receipt_id"] is None:
        raise NarrativeContractError("model unit requires model receipt")
    return {"schema": "narrative-unit/v1", "unit_id": _id(value["unit_id"], "unit_id"), "unit_kind": value["unit_kind"], "title": _text(value["title"], "title"), "summary": _text(value["summary"], "summary", empty=True), "order": _integer(value["order"], "order"), "evidence_spans": normalized_spans, "source_attributions": normalized_attrs, "relations": normalized_relations, "provenance": {"source_mode": provenance["source_mode"], "model_receipt_id": None if provenance["model_receipt_id"] is None else _id(provenance["model_receipt_id"], "model_receipt_id")}}


PLAN_FIELDS = frozenset({"schema", "plan_id", "version", "template_binding", "unit_refs", "hierarchy", "created_from_snapshot_hash", "immutable"})
TEMPLATE_FIELDS = frozenset({"data_plugin_id", "data_release_id", "package_hash", "format_id", "template_id"})
UNIT_REF_FIELDS = frozenset({"unit_id", "payload_asset_id", "payload_hash", "order", "evidence_span_ids", "source_attribution_ids"})
HIERARCHY_FIELDS = frozenset({"node_id", "parent_node_id", "level", "title", "unit_ids", "order"})


def interpret_plot_template(template: Mapping[str, Any], hierarchy: Sequence[Mapping[str, Any]], units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Interpret the selected immutable plot template against a plan.

    ``narrative-plan/v1`` intentionally keeps the public payload small and
    does not duplicate the Data package.  That does not mean the template is
    advisory: compilation must execute its hierarchy, ordering and required
    beat constraints before emitting a plan.  The returned value is a
    deterministic, internal interpretation record used by the runtime and
    tests; callers must not persist it as a second authority.

    A required beat is represented by a non-empty hierarchy node at its target
    level.  Multiple beats may intentionally share a level/node (for example,
    a compact chapter node can carry both progressive complications and the
    crisis choice), but every required target level must be present.  This
    rejects the common one-book-node/empty-hierarchy shortcut without forcing
    a particular authoring granularity.  Optional beats may be omitted.
    """
    if not isinstance(template, Mapping):
        raise NarrativeContractError("selected plot template must be an object")
    required_template = {"template_id", "display_name", "levels", "beats", "constraints"}
    if set(template) != required_template:
        raise NarrativeContractError("selected plot template is not closed")
    levels = template["levels"]
    if levels != list(PLAN_LEVELS):
        raise NarrativeContractError("template hierarchy levels are not canonical")
    constraints = template["constraints"]
    if not isinstance(constraints, Mapping) or set(constraints) != {"ordered", "allow_optional_beats", "requires_evidence_closure", "authority"}:
        raise NarrativeContractError("template constraints are not closed")
    if constraints["ordered"] is not True or constraints["requires_evidence_closure"] is not True or constraints["authority"] != "template_only":
        raise NarrativeContractError("template constraints are not enforceable")
    if constraints["allow_optional_beats"] is not True:
        raise NarrativeContractError("template optional-beat policy is invalid")
    beats = template["beats"]
    if not isinstance(beats, list) or not beats:
        raise NarrativeContractError("template beats are absent")
    beat_fields = {"beat_id", "label_zh", "order", "target_level", "purpose", "required"}
    normalized_beats: list[dict[str, Any]] = []
    seen_beats: set[str] = set()
    previous_order: int | None = None
    for index, raw in enumerate(beats):
        if not isinstance(raw, Mapping) or set(raw) != beat_fields:
            raise NarrativeContractError(f"template beats[{index}] is not closed")
        beat_id = raw["beat_id"]
        if not isinstance(beat_id, str) or not beat_id or beat_id in seen_beats:
            raise NarrativeContractError("template beat identity is invalid")
        target = raw["target_level"]
        if target not in LEVEL_INDEX:
            raise NarrativeContractError("template beat target level is invalid")
        order = raw["order"]
        if isinstance(order, bool) or not isinstance(order, int) or (previous_order is not None and order <= previous_order):
            raise NarrativeContractError("template beat ordering is invalid")
        if not isinstance(raw["required"], bool) or not isinstance(raw["purpose"], str) or not raw["purpose"]:
            raise NarrativeContractError("template beat semantics are invalid")
        seen_beats.add(beat_id)
        previous_order = order
        normalized_beats.append(dict(raw))

    # Validate the hierarchy using the same closed graph rules as the plan
    # contract, then enforce the template's required level sequence.
    unit_ids = {str(item.get("unit_id")) for item in units if isinstance(item, Mapping)}
    if not unit_ids or any(not isinstance(item, Mapping) for item in units):
        raise NarrativeContractError("template interpretation requires Units")
    normalized_hierarchy = _validate_hierarchy(hierarchy, unit_ids)
    ordered_nodes = sorted(normalized_hierarchy, key=lambda item: (item["order"], item["node_id"]))
    # The template's ``ordered`` constraint applies to the visible tree as
    # well as to Units.  A node at a coarser level cannot appear after one at
    # a deeper level in the deterministic traversal; parent links alone do
    # not prevent that malformed ordering.
    previous_level = -1
    for node in ordered_nodes:
        level_index = LEVEL_INDEX[node["level"]]
        if level_index < previous_level:
            raise NarrativeContractError("template ordered constraint is violated by hierarchy level order")
        previous_level = level_index
    for node in ordered_nodes:
        if not node["unit_ids"]:
            # Empty nodes do not satisfy a required beat and are not useful as
            # an interpreted structure.  Keep the failure deterministic.
            continue
        if any(not isinstance(item, Mapping) or not item.get("evidence_spans") or not item.get("source_attributions") for item in units if item.get("unit_id") in set(node["unit_ids"])):
            raise NarrativeContractError("template hierarchy node lacks evidence closure")
    required = [beat for beat in normalized_beats if beat["required"]]
    node_levels = [node["level"] for node in ordered_nodes if node["unit_ids"]]
    assignments: list[dict[str, Any]] = []
    for beat in required:
        target = beat["target_level"]
        found = next(((position, node) for position, node in enumerate(ordered_nodes) if node["level"] == target and node["unit_ids"]), None)
        if found is None:
            raise NarrativeContractError(f"required template beat {beat['beat_id']} is not represented by hierarchy")
        assignments.append({"beat_id": beat["beat_id"], "target_level": target, "node_id": found[1]["node_id"], "unit_ids": list(found[1]["unit_ids"])})

    # ``ordered`` also applies to Unit order within the interpreted tree.
    unit_by_id = {item["unit_id"]: item for item in units if isinstance(item, Mapping)}
    flattened = [unit_by_id[unit_id]["order"] for node in ordered_nodes for unit_id in node["unit_ids"]]
    if flattened != sorted(flattened):
        raise NarrativeContractError("template ordered constraint is violated by Unit order")
    return {"template_id": template["template_id"], "required_beats": assignments, "hierarchy_levels": node_levels, "constraints": dict(constraints)}


def _validate_hierarchy(hierarchy: Any, unit_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(hierarchy, list) or not hierarchy:
        raise NarrativeContractError("narrative plan requires hierarchy")
    normalized: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    orders: set[int] = set()
    for i, raw_node in enumerate(hierarchy):
        node = _closed(raw_node, HIERARCHY_FIELDS, f"hierarchy[{i}]")
        node_id = _id(node["node_id"], f"hierarchy[{i}].node_id")
        if node_id in node_ids or node["level"] not in LEVEL_INDEX:
            raise NarrativeContractError("hierarchy identity/level invalid")
        if node["parent_node_id"] == node_id:
            raise NarrativeContractError("hierarchy self-parent is invalid")
        order = _integer(node["order"], f"hierarchy[{i}].order")
        if order in orders:
            raise NarrativeContractError("hierarchy order must be unique")
        orders.add(order)
        raw_units = node["unit_ids"]
        if not isinstance(raw_units, list) or len(raw_units) != len(set(raw_units)):
            raise NarrativeContractError("hierarchy unit_ids are invalid")
        units = [_id(item, f"hierarchy[{i}].unit_ids") for item in raw_units]
        if any(item not in unit_ids for item in units):
            raise NarrativeContractError("hierarchy references unknown unit")
        node_ids.add(node_id)
        normalized.append({"node_id": node_id, "parent_node_id": None if node["parent_node_id"] is None else _id(node["parent_node_id"], "parent_node_id"), "level": node["level"], "title": _text(node["title"], "title"), "unit_ids": units, "order": order})
    roots = [node for node in normalized if node["parent_node_id"] is None]
    if len(roots) != 1 or roots[0]["level"] != "book":
        raise NarrativeContractError("hierarchy requires one book root")
    by_id = {node["node_id"]: node for node in normalized}
    for node in normalized:
        parent_id = node["parent_node_id"]
        if parent_id is not None:
            if parent_id not in by_id:
                raise NarrativeContractError("hierarchy parent missing")
            if LEVEL_INDEX[by_id[parent_id]["level"]] >= LEVEL_INDEX[node["level"]]:
                raise NarrativeContractError("hierarchy parent level must precede child level")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise NarrativeContractError("hierarchy cycle detected")
        if node_id in visited:
            return
        visiting.add(node_id)
        parent = by_id[node_id]["parent_node_id"]
        if parent is not None:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in normalized:
        visit(node["node_id"])
    covered = [unit for node in normalized for unit in node["unit_ids"]]
    if set(covered) != unit_ids or len(covered) != len(set(covered)):
        raise NarrativeContractError("hierarchy does not cover each Unit exactly once")
    return normalized


def compile_narrative_plan(*, plan_id: str, version: int, template_binding: Mapping[str, Any], unit_refs: Sequence[Mapping[str, Any]], hierarchy: Sequence[Mapping[str, Any]], run_snapshot_hash: str) -> dict[str, Any]:
    return validate_narrative_plan({"schema": "narrative-plan/v1", "plan_id": plan_id, "version": version, "template_binding": dict(template_binding), "unit_refs": [dict(x) for x in unit_refs], "hierarchy": [dict(x) for x in hierarchy], "created_from_snapshot_hash": run_snapshot_hash, "immutable": True})


def validate_narrative_plan(raw: Any) -> dict[str, Any]:
    value = _closed(raw, PLAN_FIELDS, "narrative_plan")
    if value["schema"] == "plugin-plan/v1":
        raise NarrativeContractError("business narrative-plan/v1 must not be plugin-plan/v1")
    if value["schema"] != "narrative-plan/v1" or value["immutable"] is not True:
        raise NarrativeContractError("narrative plan schema/immutability invalid")
    template = _closed(value["template_binding"], TEMPLATE_FIELDS, "template_binding")
    template = {"data_plugin_id": _id(template["data_plugin_id"], "data_plugin_id"), "data_release_id": _hash(template["data_release_id"], "data_release_id"), "package_hash": _hash(template["package_hash"], "package_hash"), "format_id": _text(template["format_id"], "format_id"), "template_id": _id(template["template_id"], "template_id")}
    if template["format_id"] != "plot-structure-template/v1":
        raise NarrativeContractError("template format must be plot-structure-template/v1")
    refs = value["unit_refs"]
    if not isinstance(refs, list) or not refs:
        raise NarrativeContractError("narrative plan requires unit_refs")
    normalized_refs: list[dict[str, Any]] = []
    unit_ids: list[str] = []
    orders: list[int] = []
    for i, raw_ref in enumerate(refs):
        ref = _closed(raw_ref, UNIT_REF_FIELDS, f"unit_refs[{i}]")
        lists: dict[str, list[str]] = {}
        for list_field in ("evidence_span_ids", "source_attribution_ids"):
            if not isinstance(ref[list_field], list) or not ref[list_field] or len(ref[list_field]) != len(set(ref[list_field])):
                raise NarrativeContractError(f"unit_refs[{i}].{list_field} invalid")
            lists[list_field] = [_id(item, f"{list_field} item") for item in ref[list_field]]
        normalized = {"unit_id": _id(ref["unit_id"], "unit_id"), "payload_asset_id": _id(ref["payload_asset_id"], "payload_asset_id"), "payload_hash": _hash(ref["payload_hash"], "payload_hash"), "order": _integer(ref["order"], "order"), "evidence_span_ids": lists["evidence_span_ids"], "source_attribution_ids": lists["source_attribution_ids"]}
        normalized_refs.append(normalized)
        unit_ids.append(normalized["unit_id"])
        orders.append(normalized["order"])
    if len(unit_ids) != len(set(unit_ids)) or len(orders) != len(set(orders)):
        raise NarrativeContractError("unit identity/order must be unique")
    normalized_hierarchy = _validate_hierarchy(value["hierarchy"], set(unit_ids))
    return {"schema": "narrative-plan/v1", "plan_id": _id(value["plan_id"], "plan_id"), "version": _integer(value["version"], "version", 1), "template_binding": template, "unit_refs": normalized_refs, "hierarchy": normalized_hierarchy, "created_from_snapshot_hash": _hash(value["created_from_snapshot_hash"], "created_from_snapshot_hash"), "immutable": True}


SYNTHESIS_FIELDS = frozenset({"schema", "synthesis_id", "title", "summary", "plan_ref", "sections", "evidence_spans", "source_attributions", "broker_children"})
PLAN_REF_FIELDS = frozenset({"plan_id", "payload_asset_id", "payload_hash"})
SECTION_FIELDS = frozenset({"section_id", "title", "summary", "unit_ids", "evidence_span_ids", "source_attribution_ids"})
CHILD_FIELDS = frozenset({"binding_id", "child_job_id", "child_run_snapshot_hash", "result_contract", "result_bundle_asset_id", "result_bundle_hash", "provenance_receipt_id", "broker_invocation_asset_id", "broker_invocation_hash"})


def validate_narrative_synthesis(raw: Any, *, expected_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = _closed(raw, SYNTHESIS_FIELDS, "narrative_synthesis")
    if value["schema"] != "narrative-synthesis/v1":
        raise NarrativeContractError("synthesis schema invalid")
    spans = value["evidence_spans"]
    if not isinstance(spans, list) or not spans:
        raise NarrativeContractError("synthesis must preserve EvidenceSpan")
    normalized_spans: list[dict[str, Any]] = []
    span_ids: set[str] = set()
    for i, span in enumerate(spans):
        normalized = _normalise_span_without_source(span, f"evidence_spans[{i}]")
        if normalized["evidence_span_id"] in span_ids:
            raise NarrativeContractError("duplicate synthesis EvidenceSpan")
        span_ids.add(normalized["evidence_span_id"])
        normalized_spans.append(normalized)
    attrs = value["source_attributions"]
    if not isinstance(attrs, list) or not attrs:
        raise NarrativeContractError("synthesis must preserve per-source attribution")
    normalized_attrs = [validate_source_attribution(item, span_ids, label=f"source_attributions[{i}]") for i, item in enumerate(attrs)]
    attr_ids = [item["attribution_id"] for item in normalized_attrs]
    if len(attr_ids) != len(set(attr_ids)):
        raise NarrativeContractError("duplicate synthesis attribution")
    children = value["broker_children"]
    if not isinstance(children, list) or not children:
        raise NarrativeContractError("synthesis requires Broker child receipts")
    normalized_children: list[dict[str, Any]] = []
    receipt_ids: set[str] = set()
    binding_ids: set[str] = set()
    for i, raw_child in enumerate(children):
        child = _closed(raw_child, CHILD_FIELDS, f"broker_children[{i}]")
        if child["result_contract"] not in {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}:
            raise NarrativeContractError("child result contract invalid")
        normalized = {"binding_id": _id(child["binding_id"], "binding_id"), "child_job_id": _id(child["child_job_id"], "child_job_id"), "child_run_snapshot_hash": _hash(child["child_run_snapshot_hash"], "child_run_snapshot_hash"), "result_contract": child["result_contract"], "result_bundle_asset_id": _id(child["result_bundle_asset_id"], "result_bundle_asset_id"), "result_bundle_hash": _hash(child["result_bundle_hash"], "result_bundle_hash"), "provenance_receipt_id": _id(child["provenance_receipt_id"], "provenance_receipt_id"), "broker_invocation_asset_id": _id(child["broker_invocation_asset_id"], "broker_invocation_asset_id"), "broker_invocation_hash": _hash(child["broker_invocation_hash"], "broker_invocation_hash")}
        if normalized["binding_id"] in binding_ids or normalized["provenance_receipt_id"] in receipt_ids:
            raise NarrativeContractError("duplicate Broker child identity")
        binding_ids.add(normalized["binding_id"])
        receipt_ids.add(normalized["provenance_receipt_id"])
        normalized_children.append(normalized)
    if any(attr["child_receipt_id"] is not None and attr["child_receipt_id"] not in receipt_ids for attr in normalized_attrs):
        raise NarrativeContractError("source attribution child receipt is absent")
    sections = value["sections"]
    if not isinstance(sections, list) or not sections:
        raise NarrativeContractError("synthesis requires sections")
    normalized_sections: list[dict[str, Any]] = []
    section_ids: set[str] = set()
    section_units: list[str] = []
    for i, raw_section in enumerate(sections):
        section = _closed(raw_section, SECTION_FIELDS, f"sections[{i}]")
        section_id = _id(section["section_id"], "section_id")
        if section_id in section_ids:
            raise NarrativeContractError("duplicate synthesis section")
        section_ids.add(section_id)
        if not isinstance(section["unit_ids"], list) or not section["unit_ids"] or len(section["unit_ids"]) != len(set(section["unit_ids"])):
            raise NarrativeContractError("section requires unique units")
        units = [_id(item, "section.unit_id") for item in section["unit_ids"]]
        for field, known in (("evidence_span_ids", span_ids), ("source_attribution_ids", set(attr_ids))):
            if not isinstance(section[field], list) or not section[field] or len(section[field]) != len(set(section[field])) or any(_id(item, field) not in known for item in section[field]):
                raise NarrativeContractError(f"section {field} is not source-closed")
        section_units.extend(units)
        normalized_sections.append({"section_id": section_id, "title": _text(section["title"], "title"), "summary": _text(section["summary"], "summary", empty=True), "unit_ids": units, "evidence_span_ids": list(section["evidence_span_ids"]), "source_attribution_ids": list(section["source_attribution_ids"])})
    if expected_plan is not None:
        expected_refs = {item["unit_id"]: item for item in expected_plan["unit_refs"]}
        expected_ids = set(expected_refs)
        if set(section_units) != expected_ids or len(section_units) != len(expected_ids):
            raise NarrativeContractError("synthesis sections do not cover plan Units exactly once")
        # Section-level references must be the union of the references carried
        # by the exact Units assigned to that section.  Merely pointing at a
        # globally known span/attribution is insufficient: otherwise a model
        # could silently move evidence between Units while retaining valid
        # identities.
        for index, section in enumerate(normalized_sections):
            section_refs = [expected_refs[unit_id] for unit_id in section["unit_ids"]]
            expected_span_ids = {
                span_id for ref in section_refs for span_id in ref["evidence_span_ids"]
            }
            expected_attr_ids = {
                attr_id for ref in section_refs for attr_id in ref["source_attribution_ids"]
            }
            if (set(section["evidence_span_ids"]) != expected_span_ids
                    or set(section["source_attribution_ids"]) != expected_attr_ids):
                raise NarrativeContractError(
                    f"section {index} source references do not close over assigned Units"
                )
    plan_ref = _closed(value["plan_ref"], PLAN_REF_FIELDS, "plan_ref")
    normalized_plan_ref = {"plan_id": _id(plan_ref["plan_id"], "plan_id"), "payload_asset_id": _id(plan_ref["payload_asset_id"], "payload_asset_id"), "payload_hash": _hash(plan_ref["payload_hash"], "payload_hash")}
    return {"schema": "narrative-synthesis/v1", "synthesis_id": _id(value["synthesis_id"], "synthesis_id"), "title": _text(value["title"], "title"), "summary": _text(value["summary"], "summary", empty=True), "plan_ref": normalized_plan_ref, "sections": normalized_sections, "evidence_spans": normalized_spans, "source_attributions": normalized_attrs, "broker_children": normalized_children}


__all__ = ["EvidenceSpanError", "NarrativeContractError", "build_evidence_span", "canonical_json_bytes", "compile_narrative_plan", "hash_json", "interpret_plot_template", "sha256_text", "validate_evidence_span", "validate_narrative_plan", "validate_narrative_synthesis", "validate_narrative_unit", "validate_nodes", "validate_source_attribution"]
