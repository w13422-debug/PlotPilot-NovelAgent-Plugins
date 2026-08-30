"""Fail-closed business contracts for the donor-analysis plugin.

The module deliberately contains no persistence or Host access.  Core remains
the authority for Assets, Candidates, acceptance state and Publication; these
helpers only validate immutable snapshots supplied to the plugin.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _sdk_canonical_bytes
except ImportError:  # pragma: no cover - pure contract documentation use
    _sdk_canonical_bytes = None


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ATOM_KINDS = frozenset(
    {
        "action", "character_voice", "climax", "concealment", "conflict",
        "decision", "description", "dialogue", "emotion", "foreshadow", "hook",
        "inner_monologue", "other", "payoff", "promise", "relationship_change",
        "rhythm", "scene_turn", "setup", "signature_action", "suspense",
        "scenery", "appearance", "combat", "psychology", "environment", "object",
        "line_drawing", "fine_detail", "stream_of_consciousness", "symbolism",
        "metaphor", "contrast", "action_ratio", "psychology_ratio", "scenery_ratio",
        "chapter_rhythm", "emotion_curve", "climax_buffer_sequence",
        "lexicon_and_idiom", "viewpoint", "grammatical_person", "tense",
        "chapter_ending", "sentence_length", "paragraph_length", "segmentation",
        "punctuation_habits",
    }
)
OBSERVATION_CONFIDENCE = frozenset({"confirmed", "uncertain", "conflict"})
INTERPRETATION_CONFIDENCE = frozenset({"inferred", "uncertain", "conflict"})
SOURCE_CATEGORIES = frozenset(
    {"original_observation", "user_annotation", "inferred_candidate"}
)
DONOR_PLUGIN_ID = "com.plotpilot.novelagent.donor-analysis"
ATOM_EXTRACT_CAPABILITY_ID = "analysis.book.atom.extract/v1"
ATOM_MANUAL_CAPABILITY_ID = "analysis.book.atom.manual/v1"
ATOM_SKILL_ID = "com.plotpilot.skill.donor.atomic-breakdown"
ATOM_SKILL_PACKAGE_HASH = "27930699e02a937e4f56cf5f90202adf6770cbe8a58391ced3e6801ee39b5339"
ATOM_SKILL_RELEASE_ID = "ed9fad4c3287f8a71c47b01344d1099862578de98289e5cecaf9b890a1c8bcfa"


class DonorContractError(ValueError):
    """Raised when an authority or payload contract does not close."""


class EvidenceSpanError(DonorContractError):
    """Raised for an invalid Unicode scalar EvidenceSpan."""


def _closed(value: Any, fields: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DonorContractError(f"{label} must be an object")
    result = dict(value)
    if set(result) != set(fields):
        missing = sorted(set(fields) - set(result))
        extra = sorted(set(result) - set(fields))
        raise DonorContractError(
            f"{label} fields are not closed: missing={missing}, extra={extra}"
        )
    return result


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise DonorContractError(f"{label} must be {'a string' if allow_empty else 'non-empty'}")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise DonorContractError(f"{label} contains a surrogate code point") from exc
    return value


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", text) is None:
        raise DonorContractError(f"{label} must be a Core identifier")
    return text


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise DonorContractError(f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DonorContractError(f"{label} must be an integer >= {minimum}")
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(_text(value, "text", allow_empty=True).encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    if _sdk_canonical_bytes is not None:
        return bytes(_sdk_canonical_bytes(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def hash_json(schema: str, value: Any) -> str:
    return hashlib.sha256(schema.encode("ascii") + b"\n" + canonical_json_bytes(value)).hexdigest()


EVIDENCE_FIELDS = frozenset(
    {
        "schema", "workspace_id", "document_id", "revision_id", "node_id",
        "start_codepoint", "end_codepoint", "quote", "quote_hash",
        "canonical_text_hash",
    }
)
NODE_FIELDS = frozenset({"node_id", "start_codepoint", "end_codepoint"})


def validate_nodes(nodes: Any, text_length: int) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or not nodes:
        raise DonorContractError("nodes must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(nodes):
        node = _closed(raw, NODE_FIELDS, f"nodes[{index}]")
        node_id = _identifier(node["node_id"], f"nodes[{index}].node_id")
        start = _integer(node["start_codepoint"], f"nodes[{index}].start_codepoint")
        end = _integer(node["end_codepoint"], f"nodes[{index}].end_codepoint", 1)
        if node_id in seen or start >= end or end > text_length:
            raise DonorContractError(f"nodes[{index}] has duplicate identity or invalid bounds")
        seen.add(node_id)
        result.append({"node_id": node_id, "start_codepoint": start, "end_codepoint": end})
    return result


def build_evidence_span(
    *, workspace_id: str, document_id: str, revision_id: str, node_id: str,
    start_codepoint: int, end_codepoint: int, canonical_text: str,
) -> dict[str, Any]:
    text = _text(canonical_text, "canonical_text", allow_empty=True)
    start = _integer(start_codepoint, "start_codepoint")
    end = _integer(end_codepoint, "end_codepoint", 1)
    if start >= end or end > len(text):
        raise EvidenceSpanError("EvidenceSpan bounds are outside the canonical Revision")
    quote = text[start:end]
    return {
        "schema": "evidence-span/v1",
        "workspace_id": _identifier(workspace_id, "workspace_id"),
        "document_id": _identifier(document_id, "document_id"),
        "revision_id": _identifier(revision_id, "revision_id"),
        "node_id": _identifier(node_id, "node_id"),
        "start_codepoint": start,
        "end_codepoint": end,
        "quote": quote,
        "quote_hash": sha256_text(quote),
        "canonical_text_hash": sha256_text(text),
    }


def validate_evidence_span(
    raw: Any, canonical_text: str, nodes: Sequence[Mapping[str, Any]], *,
    workspace_id: str, document_id: str, revision_id: str,
    canonical_text_hash: str,
) -> dict[str, Any]:
    try:
        span = _closed(raw, EVIDENCE_FIELDS, "EvidenceSpan")
        if span["schema"] != "evidence-span/v1":
            raise EvidenceSpanError("EvidenceSpan schema is invalid")
        expected_owner = (workspace_id, document_id, revision_id)
        actual_owner = (span["workspace_id"], span["document_id"], span["revision_id"])
        if actual_owner != expected_owner:
            raise EvidenceSpanError("EvidenceSpan owner/Revision binding changed")
        _identifier(span["node_id"], "EvidenceSpan.node_id")
        start = _integer(span["start_codepoint"], "EvidenceSpan.start_codepoint")
        end = _integer(span["end_codepoint"], "EvidenceSpan.end_codepoint", 1)
        text = _text(canonical_text, "canonical_text", allow_empty=True)
        expected_hash = _hash(canonical_text_hash, "canonical_text_hash")
        if sha256_text(text) != expected_hash or span["canonical_text_hash"] != expected_hash:
            raise EvidenceSpanError("EvidenceSpan canonical Revision hash changed")
        if start >= end or end > len(text):
            raise EvidenceSpanError("EvidenceSpan bounds are invalid")
        quote = _text(span["quote"], "EvidenceSpan.quote")
        if text[start:end] != quote or _hash(span["quote_hash"], "quote_hash") != sha256_text(quote):
            raise EvidenceSpanError("EvidenceSpan quote/slice/hash mismatch")
        node = next((dict(item) for item in nodes if item.get("node_id") == span["node_id"]), None)
        if node is None:
            raise EvidenceSpanError("EvidenceSpan node does not exist")
        if node["start_codepoint"] > start or node["end_codepoint"] < end:
            raise EvidenceSpanError("EvidenceSpan is not contained by its node")
        return deepcopy(span)
    except EvidenceSpanError:
        raise
    except Exception as exc:
        raise EvidenceSpanError(str(exc)) from exc


METHOD_FIELDS = frozenset({"name", "version"})
PROVENANCE_FIELDS = frozenset(
    {
        "schema", "mode", "method", "model", "skill", "plugin", "run",
        "run_snapshot_hash", "source_revision", "manual_annotation", "created_at",
    }
)
MODEL_PROVENANCE_FIELDS = frozenset({"profile_revision_id", "receipt_id"})
SKILL_PROVENANCE_FIELDS = frozenset({"skill_id", "package_hash", "release_id"})
PLUGIN_PROVENANCE_FIELDS = frozenset(
    {"plugin_id", "package_hash", "release_id", "capability_id"}
)
RUN_PROVENANCE_FIELDS = frozenset(
    {
        "job_id", "step_id", "attempt_id", "worker_run_id", "lease_epoch",
        "provenance_receipt_id",
    }
)
SOURCE_REVISION_PROVENANCE_FIELDS = frozenset(
    {"workspace_id", "document_id", "revision_id", "canonical_text_hash"}
)
MANUAL_ANNOTATION_FIELDS = frozenset({"actor_id", "asset_id", "asset_hash"})
ATOM_FIELDS = frozenset(
    {
        "schema", "atom_kind", "title", "observation", "interpretation",
        "applicability", "limitations", "source_category",
        "observation_confidence", "interpretation_confidence", "evidence_spans",
        "tags", "analysis_method", "provenance", "prompt_eligible",
        "promotion_authorized", "authority",
    }
)


def build_atom_provenance(
    *, mode: str, analysis_method: Mapping[str, Any], plugin_id: str,
    package_hash: str, release_id: str, capability_id: str, job_id: str,
    step_id: str, attempt_id: str, worker_run_id: str, lease_epoch: int,
    provenance_receipt_id: str, run_snapshot_hash: str, workspace_id: str,
    document_id: str, source_revision_id: str, canonical_text_hash: str,
    created_at: str, model_profile_revision_id: str | None = None,
    model_receipt_id: str | None = None, actor_id: str | None = None,
    annotation_asset_id: str | None = None,
    annotation_asset_hash: str | None = None,
) -> dict[str, Any]:
    """Build trusted, closed Atom provenance from Host-bound references.

    The atomic-breakdown Skill identity is deliberately fixed by this contract;
    callers cannot substitute a different Skill while retaining book-atom/v1.
    """
    method = _closed(analysis_method, METHOD_FIELDS, "Atom.analysis_method")
    result: dict[str, Any] = {
        "schema": "book-atom-provenance/v1",
        "mode": mode,
        "method": deepcopy(method),
        "model": None if model_profile_revision_id is None and model_receipt_id is None else {
            "profile_revision_id": model_profile_revision_id,
            "receipt_id": model_receipt_id,
        },
        "skill": {
            "skill_id": ATOM_SKILL_ID,
            "package_hash": ATOM_SKILL_PACKAGE_HASH,
            "release_id": ATOM_SKILL_RELEASE_ID,
        },
        "plugin": {
            "plugin_id": plugin_id,
            "package_hash": package_hash,
            "release_id": release_id,
            "capability_id": capability_id,
        },
        "run": {
            "job_id": job_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "worker_run_id": worker_run_id,
            "lease_epoch": lease_epoch,
            "provenance_receipt_id": provenance_receipt_id,
        },
        "run_snapshot_hash": run_snapshot_hash,
        "source_revision": {
            "workspace_id": workspace_id,
            "document_id": document_id,
            "revision_id": source_revision_id,
            "canonical_text_hash": canonical_text_hash,
        },
        "manual_annotation": None if actor_id is None and annotation_asset_id is None and annotation_asset_hash is None else {
            "actor_id": actor_id,
            "asset_id": annotation_asset_id,
            "asset_hash": annotation_asset_hash,
        },
        "created_at": created_at,
    }
    validate_atom_provenance(
        result,
        analysis_method=method,
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=source_revision_id,
        canonical_text_hash=canonical_text_hash,
        expected_mode=mode,
    )
    return result


def validate_atom_provenance(
    raw: Any, *, analysis_method: Mapping[str, Any], workspace_id: str,
    document_id: str, revision_id: str, canonical_text_hash: str,
    expected_mode: str | None = None,
) -> dict[str, Any]:
    provenance = _closed(raw, PROVENANCE_FIELDS, "Atom.provenance")
    if provenance["schema"] != "book-atom-provenance/v1":
        raise DonorContractError("Atom provenance schema is invalid")
    mode = provenance["mode"]
    if mode not in {"model", "manual"} or (expected_mode is not None and mode != expected_mode):
        raise DonorContractError("Atom provenance mode changed")
    method = _closed(provenance["method"], METHOD_FIELDS, "Atom.provenance.method")
    if method != dict(analysis_method):
        raise DonorContractError("Atom provenance method differs from analysis_method")
    expected_method = "book-atom-extract" if mode == "model" else "book-atom-manual"
    if method != {"name": expected_method, "version": "1"}:
        raise DonorContractError("Atom provenance method/mode binding is invalid")

    model = provenance["model"]
    if mode == "model":
        model = _closed(model, MODEL_PROVENANCE_FIELDS, "Atom.provenance.model")
        _identifier(model["profile_revision_id"], "Atom.provenance.model.profile_revision_id")
        _identifier(model["receipt_id"], "Atom.provenance.model.receipt_id")
    elif model is not None:
        raise DonorContractError("manual Atom provenance must explicitly use model=null")

    skill = _closed(provenance["skill"], SKILL_PROVENANCE_FIELDS, "Atom.provenance.skill")
    if skill != {
        "skill_id": ATOM_SKILL_ID,
        "package_hash": ATOM_SKILL_PACKAGE_HASH,
        "release_id": ATOM_SKILL_RELEASE_ID,
    }:
        raise DonorContractError("Atom provenance Skill identity changed")

    plugin = _closed(provenance["plugin"], PLUGIN_PROVENANCE_FIELDS, "Atom.provenance.plugin")
    expected_capability = ATOM_EXTRACT_CAPABILITY_ID if mode == "model" else ATOM_MANUAL_CAPABILITY_ID
    if plugin["plugin_id"] != DONOR_PLUGIN_ID or plugin["capability_id"] != expected_capability:
        raise DonorContractError("Atom provenance plugin/capability binding changed")
    _hash(plugin["package_hash"], "Atom.provenance.plugin.package_hash")
    _hash(plugin["release_id"], "Atom.provenance.plugin.release_id")

    run = _closed(provenance["run"], RUN_PROVENANCE_FIELDS, "Atom.provenance.run")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "provenance_receipt_id"):
        _identifier(run[field], f"Atom.provenance.run.{field}")
    _integer(run["lease_epoch"], "Atom.provenance.run.lease_epoch", 1)
    _hash(provenance["run_snapshot_hash"], "Atom.provenance.run_snapshot_hash")

    source = _closed(
        provenance["source_revision"],
        SOURCE_REVISION_PROVENANCE_FIELDS,
        "Atom.provenance.source_revision",
    )
    expected_source = {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "revision_id": revision_id,
        "canonical_text_hash": canonical_text_hash,
    }
    if source != expected_source:
        raise DonorContractError("Atom provenance source Revision binding changed")
    _hash(source["canonical_text_hash"], "Atom.provenance.source_revision.canonical_text_hash")

    annotation = provenance["manual_annotation"]
    if mode == "manual":
        annotation = _closed(annotation, MANUAL_ANNOTATION_FIELDS, "Atom.provenance.manual_annotation")
        _identifier(annotation["actor_id"], "Atom.provenance.manual_annotation.actor_id")
        _identifier(annotation["asset_id"], "Atom.provenance.manual_annotation.asset_id")
        _hash(annotation["asset_hash"], "Atom.provenance.manual_annotation.asset_hash")
    elif annotation is not None:
        raise DonorContractError("model Atom provenance must explicitly use manual_annotation=null")
    _text(provenance["created_at"], "Atom.provenance.created_at")
    return deepcopy(provenance)


def validate_atom_payload(
    raw: Any, *, canonical_text: str, nodes: Sequence[Mapping[str, Any]],
    workspace_id: str, document_id: str, revision_id: str,
    canonical_text_hash: str, expected_mode: str | None = None,
    allowed_atom_kinds: set[str] | frozenset[str] | None = None,
    expected_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    atom = _closed(raw, ATOM_FIELDS, "book Atom")
    if atom["schema"] != "book-atom/v1" or atom["authority"] != "candidate_only":
        raise DonorContractError("Atom must be a candidate-only book-atom/v1")
    allowed = ATOM_KINDS if allowed_atom_kinds is None else frozenset(allowed_atom_kinds)
    if atom["atom_kind"] not in allowed:
        raise DonorContractError("Atom kind is outside book-analysis-taxonomy/v1")
    for field in ("title", "observation"):
        _text(atom[field], f"Atom.{field}")
    for field in ("interpretation", "applicability", "limitations"):
        _text(atom[field], f"Atom.{field}", allow_empty=True)
    if atom["source_category"] not in SOURCE_CATEGORIES:
        raise DonorContractError("Atom source_category is invalid")
    if atom["observation_confidence"] not in OBSERVATION_CONFIDENCE:
        raise DonorContractError("Atom observation_confidence is invalid")
    if atom["interpretation_confidence"] not in INTERPRETATION_CONFIDENCE:
        raise DonorContractError("Atom interpretation_confidence is invalid")
    if not isinstance(atom["prompt_eligible"], bool) or atom["prompt_eligible"]:
        raise DonorContractError("Atom prompt_eligible must remain false")
    if not isinstance(atom["promotion_authorized"], bool) or atom["promotion_authorized"]:
        raise DonorContractError("Atom promotion_authorized must remain false")
    tags = atom["tags"]
    if not isinstance(tags, list) or len(tags) != len(set(tags)) or any(not isinstance(tag, str) or not tag for tag in tags):
        raise DonorContractError("Atom tags must be unique non-empty strings")
    method = _closed(atom["analysis_method"], METHOD_FIELDS, "Atom.analysis_method")
    _text(method["name"], "analysis_method.name"); _text(method["version"], "analysis_method.version")
    provenance = validate_atom_provenance(
        atom["provenance"], analysis_method=method, workspace_id=workspace_id,
        document_id=document_id, revision_id=revision_id,
        canonical_text_hash=canonical_text_hash, expected_mode=expected_mode,
    )
    if expected_provenance is not None and provenance != dict(expected_provenance):
        raise DonorContractError("Atom provenance differs from trusted Host binding")
    if expected_mode == "manual":
        if atom["source_category"] != "user_annotation" or method["name"] != "book-atom-manual":
            raise DonorContractError("manual Atom requires user_annotation and frozen manual provenance")
    elif expected_mode == "model":
        if atom["source_category"] == "user_annotation" or method["name"] != "book-atom-extract":
            raise DonorContractError("model Atom cannot claim manual provenance")
    evidence = atom["evidence_spans"]
    if not isinstance(evidence, list):
        raise DonorContractError("Atom evidence_spans must be an array")
    validated = [validate_evidence_span(item, canonical_text, nodes, workspace_id=workspace_id,
        document_id=document_id, revision_id=revision_id, canonical_text_hash=canonical_text_hash) for item in evidence]
    if not validated:
        raise DonorContractError("every Atom requires at least one exact EvidenceSpan")
    if atom["source_category"] == "inferred_candidate" and atom["observation_confidence"] != "uncertain":
        raise DonorContractError("inferred Atom observation must remain uncertain")
    result = deepcopy(atom); result["evidence_spans"] = validated; result["provenance"] = provenance
    return result


ORDERED_ATOM_FIELDS = frozenset(
    {"ordinal", "atom_id", "payload_hash", "acceptance_ordinal", "evidence_spans"}
)
CLAIM_INPUT_FIELDS = frozenset({"schema", "source_revision_id", "ordered_atoms"})


def validate_claim_input(
    raw: Any, *, canonical_text: str, nodes: Sequence[Mapping[str, Any]],
    workspace_id: str, document_id: str, source_revision_id: str,
    canonical_text_hash: str,
) -> dict[str, Any]:
    value = _closed(raw, CLAIM_INPUT_FIELDS, "claim-input")
    if value["schema"] != "claim-input/v1" or value["source_revision_id"] != source_revision_id:
        raise DonorContractError("claim-input source Revision binding changed")
    items = value["ordered_atoms"]
    if not isinstance(items, list) or not items:
        raise DonorContractError("claim-input ordered_atoms must be non-empty")
    text = _text(canonical_text, "canonical_text", allow_empty=True)
    validated_nodes = validate_nodes(list(nodes), len(text))
    seen_ids: set[str] = set(); seen_acceptance: set[int] = set()
    validated_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        item = _closed(raw_item, ORDERED_ATOM_FIELDS, f"ordered_atoms[{index}]")
        if _integer(item["ordinal"], "ordinal") != index:
            raise DonorContractError("claim-input ordinals must be contiguous from zero")
        atom_id = _identifier(item["atom_id"], "atom_id")
        acceptance = _integer(item["acceptance_ordinal"], "acceptance_ordinal", 1)
        _hash(item["payload_hash"], "payload_hash")
        if atom_id in seen_ids or acceptance in seen_acceptance:
            raise DonorContractError("claim-input Atom/acceptance ordinal is duplicated")
        seen_ids.add(atom_id); seen_acceptance.add(acceptance)
        evidence = item["evidence_spans"]
        if not isinstance(evidence, list) or not evidence:
            raise DonorContractError("claim-input evidence_spans must be a non-empty array")
        item["evidence_spans"] = [
            validate_evidence_span(
                span, text, validated_nodes, workspace_id=workspace_id,
                document_id=document_id, revision_id=source_revision_id,
                canonical_text_hash=canonical_text_hash,
            )
            for span in evidence
        ]
        validated_items.append(item)
    result = deepcopy(value)
    result["ordered_atoms"] = validated_items
    return result


ACCEPTED_ATOM_FIELDS = frozenset(
    {"atom_id", "source_revision_id", "status", "is_current", "acceptance_ordinal",
     "payload_asset_id", "payload_hash", "evidence_spans"}
)


def bind_current_accepted_atoms(claim_input: Mapping[str, Any], accepted_atoms: Any) -> list[dict[str, Any]]:
    if not isinstance(accepted_atoms, list) or len(accepted_atoms) != len(claim_input["ordered_atoms"]):
        raise DonorContractError("accepted Atom snapshot does not match claim-input cardinality")
    result: list[dict[str, Any]] = []
    for index, (expected, raw) in enumerate(zip(claim_input["ordered_atoms"], accepted_atoms, strict=True)):
        actual = _closed(raw, ACCEPTED_ATOM_FIELDS, f"accepted_atoms[{index}]")
        if actual["status"] != "accepted" or actual["is_current"] is not True:
            raise DonorContractError("Claim may use only current accepted Atoms")
        projection = {key: actual[key] for key in ("atom_id", "payload_hash", "acceptance_ordinal", "evidence_spans")}
        expected_projection = {key: expected[key] for key in projection}
        if projection != expected_projection or actual["source_revision_id"] != claim_input["source_revision_id"]:
            raise DonorContractError("Claim Atom snapshot/current/payload/evidence binding changed")
        if not actual["evidence_spans"]:
            raise DonorContractError("accepted Atom snapshot evidence must be non-empty")
        _identifier(actual["payload_asset_id"], "payload_asset_id")
        result.append(deepcopy(actual))
    return result


CLAIM_FIELDS = frozenset(
    {"schema", "category", "title", "claim", "scope", "applicability",
     "counterexamples", "conflicts", "interpretation_confidence",
     "ordered_atom_ids", "evidence_spans", "analysis_method",
     "prompt_eligible", "promotion_authorized", "authority"}
)


def validate_claim_payload(raw: Any, *, claim_input: Mapping[str, Any]) -> dict[str, Any]:
    value = _closed(raw, CLAIM_FIELDS, "book Claim")
    if value["schema"] != "book-claim/v1" or value["authority"] != "candidate_only":
        raise DonorContractError("Claim must be a candidate-only book-claim/v1")
    if value["category"] is not None:
        _text(value["category"], "Claim.category", allow_empty=True)
    for field in ("title", "claim"):
        _text(value[field], f"Claim.{field}")
    for field in ("scope", "applicability"):
        _text(value[field], f"Claim.{field}", allow_empty=True)
    for field in ("counterexamples", "conflicts"):
        if not isinstance(value[field], list) or any(not isinstance(item, str) for item in value[field]):
            raise DonorContractError(f"Claim.{field} must be a string array")
    if value["interpretation_confidence"] not in INTERPRETATION_CONFIDENCE:
        raise DonorContractError("Claim interpretation confidence is invalid")
    expected_ids = [item["atom_id"] for item in claim_input["ordered_atoms"]]
    if value["ordered_atom_ids"] != expected_ids:
        raise DonorContractError("Claim must cite all ordered current accepted Atoms exactly")
    expected_spans = [deepcopy(span) for item in claim_input["ordered_atoms"] for span in item["evidence_spans"]]
    if value["evidence_spans"] != expected_spans:
        raise DonorContractError("Claim evidence projection differs from exact Atom evidence")
    method = _closed(value["analysis_method"], METHOD_FIELDS, "Claim.analysis_method")
    if method != {"name": "book-claim-generate", "version": "1"}:
        raise DonorContractError("Claim analysis_method is invalid")
    if value["prompt_eligible"] is not False or value["promotion_authorized"] is not False:
        raise DonorContractError("Claim promotion gates must remain false")
    return deepcopy(value)


REREVIEW_RECORD_FIELDS = frozenset(
    {"candidate_id", "candidate_kind", "status", "is_current", "payload_hash",
     "payload_asset_id", "source_revision_id", "parent_candidate_id",
     "successor_candidate_id"}
)


def validate_rereview_records(raw: Any, *, source_revision_id: str, known_parent_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise DonorContractError("candidate_records must be non-empty")
    result: list[dict[str, Any]] = []; seen: set[str] = set()
    for index, child in enumerate(raw):
        item = _closed(child, REREVIEW_RECORD_FIELDS, f"candidate_records[{index}]")
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        if candidate_id in seen or item["candidate_kind"] not in {"book_atom", "book_claim"}:
            raise DonorContractError("rereview Candidate identity/kind is invalid")
        seen.add(candidate_id)
        if item["status"] not in {"pending", "accepted", "rejected", "superseded"} or not isinstance(item["is_current"], bool):
            raise DonorContractError("rereview Candidate status/current is invalid")
        _hash(item["payload_hash"], "payload_hash"); _identifier(item["payload_asset_id"], "payload_asset_id")
        if item["source_revision_id"] != source_revision_id:
            raise DonorContractError("rereview Candidate belongs to another Revision")
        parent = item["parent_candidate_id"]; successor = item["successor_candidate_id"]
        if parent is not None:
            _identifier(parent, "parent_candidate_id")
        if successor is not None:
            _identifier(successor, "successor_candidate_id")
        result.append(deepcopy(item))
    for item in result:
        parent = item["parent_candidate_id"]
        successor = item["successor_candidate_id"]
        if parent is None or parent not in known_parent_ids:
            raise DonorContractError("rereview predecessor is not Core-known")
        if successor is not None and successor != item["candidate_id"]:
            raise DonorContractError("idempotent successor must be the existing successor Candidate itself")
    return result


REREVIEW_DIAGNOSTIC_FIELDS = frozenset(
    {"candidate_id", "severity", "code", "message", "recommendation", "successor_candidate_id"}
)


def validate_rereview_diagnostics(
    raw: Any, *, candidate_records: Sequence[Mapping[str, Any]],
    allow_successor: bool,
) -> list[dict[str, Any]]:
    """Require exactly one ordered diagnostic for every rereview input record."""
    if not isinstance(raw, list) or len(raw) != len(candidate_records) or not raw:
        raise DonorContractError(
            "rereview diagnostics must map one-to-one to all Candidate records"
        )
    if not isinstance(allow_successor, bool):
        raise DonorContractError("allow_successor must be boolean")
    result: list[dict[str, Any]] = []
    for index, (raw_item, record) in enumerate(zip(raw, candidate_records, strict=True)):
        item = _closed(raw_item, REREVIEW_DIAGNOSTIC_FIELDS, f"diagnostics[{index}]")
        expected_id = _identifier(record.get("candidate_id"), f"candidate_records[{index}].candidate_id")
        candidate_id = _identifier(item["candidate_id"], f"diagnostics[{index}].candidate_id")
        if candidate_id != expected_id:
            raise DonorContractError(
                "rereview diagnostics must preserve Candidate input order exactly"
            )
        if item["severity"] not in {"info", "warning", "error"}:
            raise DonorContractError("rereview diagnostic severity is invalid")
        _text(item["code"], f"diagnostics[{index}].code")
        _text(item["message"], f"diagnostics[{index}].message", allow_empty=True)
        _text(item["recommendation"], f"diagnostics[{index}].recommendation", allow_empty=True)
        successor = item["successor_candidate_id"]
        expected_successor = record.get("successor_candidate_id")
        if successor is not None:
            _identifier(successor, f"diagnostics[{index}].successor_candidate_id")
        # Existing Core lineage is evidence, not a model-created mutation.  It
        # must be projected exactly even when allow_successor is false; null is
        # therefore not permitted to erase an existing successor.
        if successor != expected_successor:
            raise DonorContractError(
                "rereview successor must exactly preserve the input Core lineage"
            )
        result.append(deepcopy(item))
    return result
