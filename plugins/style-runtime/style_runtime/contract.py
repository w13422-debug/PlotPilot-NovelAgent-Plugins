"""Closed Style/qualification contracts shared by run, resume and validation."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs
except ImportError:  # isolated wheel import before SDK path is attached
    def canonical_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def hash_jcs(prefix: str, value: Any) -> str:
        return hashlib.sha256(prefix.encode("ascii") + b"\n" + canonical_bytes(value)).hexdigest()


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    # SemVer 2.0.0: numeric prerelease identifiers may not contain leading
    # zeroes; identifiers containing a letter or hyphen are non-numeric.
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

STYLE_PACK_FIELDS = frozenset({
    "schema", "style_pack_id", "version", "style_release_id", "payload_hash",
    "source_cas", "target_cas", "manufacture_snapshot", "features",
    "lexicon_bindings", "constraints", "exemplar_hashes",
})
SOURCE_CAS_FIELDS = frozenset({"asset_id", "sha256", "revision_id"})
TARGET_CAS_FIELDS = frozenset({"workspace_id", "entity_id", "base_revision_id", "base_content_hash"})
SNAPSHOT_FIELDS = frozenset({
    "run_snapshot_hash", "route_id", "prompt_hash", "output_schema_hash",
    "chunk_plan_hash", "provider_attempt_ids", "checkpoint_ids", "usage",
})
USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
FEATURE_FIELDS = frozenset({"voice", "rhythm", "syntax", "imagery", "taboos"})
LEXICON_BINDING_FIELDS = frozenset({"data_plugin_id", "data_release_id", "bundle_hash", "order"})
QUALIFICATION_FIELDS = frozenset({
    "schema", "receipt_id", "style_pack_id", "style_release_id", "style_payload_hash",
    "qualification_run_snapshot_hash", "rubric_hash", "manufacturing_route_id",
    "writer_route_id", "reviewer_route_id", "manufacturing_attempt_ids",
    "writer_attempt_ids", "reviewer_attempt_ids", "case_results", "decision",
    "automatic_eligible", "created_at", "receipt_hash",
})
CASE_FIELDS = frozenset({"case_id", "writer_output_hash", "review_output_hash", "score_0_100", "passed"})
QUALIFICATION_EXTENDED_FIELDS = QUALIFICATION_FIELDS | frozenset({
    "style_pack_asset_id", "style_pack_asset_hash",
    "sealed_case_asset_id", "sealed_case_asset_hash",
    "rubric_asset_id", "rubric_asset_hash",
    "writer_output_asset_ids", "writer_output_asset_hashes",
    "review_output_asset_id", "review_output_asset_hash",
})


class StyleContractError(ValueError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_json(prefix: str, value: Mapping[str, Any]) -> str:
    return hash_jcs(prefix, dict(value))


def _exact(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        actual = sorted(value) if isinstance(value, Mapping) else type(value).__name__
        raise StyleContractError(f"{label} fields differ: expected={sorted(fields)} actual={actual}")
    return dict(value)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise StyleContractError(f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise StyleContractError(f"{label} must be lowercase SHA-256")
    return value


def _strings(value: Any, label: str, *, unique: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise StyleContractError(f"{label} must contain non-empty strings")
    if unique and len(value) != len(set(value)):
        raise StyleContractError(f"{label} must be unique")
    return list(value)


def calculate_style_payload_hash(pack: Mapping[str, Any]) -> str:
    projected = deepcopy(dict(pack))
    projected["payload_hash"] = ""
    projected["style_release_id"] = ""
    return sha256_bytes(b"style-pack/v1\n" + canonical_bytes(projected))


def calculate_style_release_id(style_pack_id: str, version: str, payload_hash: str) -> str:
    return sha256_bytes(f"style-release/v1\n{style_pack_id}\n{version}\n{payload_hash}\n".encode())


def validate_style_pack(value: Mapping[str, Any]) -> dict[str, Any]:
    pack = _exact(value, STYLE_PACK_FIELDS, "style-pack/v1")
    if pack["schema"] != "style-pack/v1":
        raise StyleContractError("style pack schema mismatch")
    _identifier(pack["style_pack_id"], "style_pack_id")
    if not isinstance(pack["version"], str) or SEMVER_RE.fullmatch(pack["version"]) is None:
        raise StyleContractError("style pack version must be SemVer")
    _hash(pack["payload_hash"], "payload_hash")
    _hash(pack["style_release_id"], "style_release_id")
    source = _exact(pack["source_cas"], SOURCE_CAS_FIELDS, "source_cas")
    _identifier(source["asset_id"], "source_cas.asset_id")
    _identifier(source["revision_id"], "source_cas.revision_id")
    _hash(source["sha256"], "source_cas.sha256")
    target = _exact(pack["target_cas"], TARGET_CAS_FIELDS, "target_cas")
    for field in ("workspace_id", "entity_id", "base_revision_id"):
        _identifier(target[field], f"target_cas.{field}")
    _hash(target["base_content_hash"], "target_cas.base_content_hash")
    snapshot = _exact(pack["manufacture_snapshot"], SNAPSHOT_FIELDS, "manufacture_snapshot")
    for field in ("run_snapshot_hash", "prompt_hash", "output_schema_hash", "chunk_plan_hash"):
        _hash(snapshot[field], f"manufacture_snapshot.{field}")
    _identifier(snapshot["route_id"], "manufacture_snapshot.route_id")
    for field in ("provider_attempt_ids", "checkpoint_ids"):
        values = _strings(snapshot[field], f"manufacture_snapshot.{field}")
        if not values:
            raise StyleContractError(f"manufacture_snapshot.{field} must be non-empty")
        for item in values:
            _identifier(item, f"manufacture_snapshot.{field} item")
    usage = _exact(snapshot["usage"], USAGE_FIELDS, "manufacture_snapshot.usage")
    for field in USAGE_FIELDS:
        if isinstance(usage[field], bool) or not isinstance(usage[field], int) or usage[field] < 0:
            raise StyleContractError(f"usage.{field} must be a non-negative integer")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise StyleContractError("usage.total_tokens mismatch")
    features = _exact(pack["features"], FEATURE_FIELDS, "features")
    for field in FEATURE_FIELDS:
        _strings(features[field], f"features.{field}")
    bindings = pack["lexicon_bindings"]
    if not isinstance(bindings, list):
        raise StyleContractError("lexicon_bindings must be an array")
    orders: list[int] = []
    for index, raw in enumerate(bindings):
        binding = _exact(raw, LEXICON_BINDING_FIELDS, f"lexicon_bindings[{index}]")
        _identifier(binding["data_plugin_id"], "data_plugin_id")
        _hash(binding["data_release_id"], "data_release_id")
        _hash(binding["bundle_hash"], "bundle_hash")
        if isinstance(binding["order"], bool) or not isinstance(binding["order"], int) or binding["order"] < 1:
            raise StyleContractError("lexicon binding order must be positive integer")
        orders.append(binding["order"])
    if orders != list(range(1, len(orders) + 1)):
        raise StyleContractError("lexicon bindings must be contiguous from one")
    for field in ("voice", "rhythm", "syntax", "imagery", "taboos"):
        if _strings(features[field], f"features.{field}") != sorted(set(features[field]), key=lambda item: item.encode("utf-8")):
            raise StyleContractError(f"features.{field} must be UTF-8 byte-sorted and unique")
    constraints = _strings(pack["constraints"], "constraints")
    if constraints != sorted(set(constraints), key=lambda item: item.encode("utf-8")):
        raise StyleContractError("constraints must be UTF-8 byte-sorted and unique")
    exemplars = pack["exemplar_hashes"]
    if not isinstance(exemplars, list) or not exemplars or exemplars != sorted(set(exemplars)):
        raise StyleContractError("exemplar_hashes must be non-empty sorted unique")
    for item in exemplars:
        _hash(item, "exemplar_hash")
    expected_payload = calculate_style_payload_hash(pack)
    if pack["payload_hash"] != expected_payload:
        raise StyleContractError("style payload_hash mismatch")
    expected_release = calculate_style_release_id(pack["style_pack_id"], pack["version"], expected_payload)
    if pack["style_release_id"] != expected_release:
        raise StyleContractError("style_release_id mismatch")
    return deepcopy(pack)


def validate_qualification_receipt(value: Mapping[str, Any], *, pack: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StyleContractError("qualification receipt must be an object")
    value = deepcopy(dict(value))
    if set(value) == set(QUALIFICATION_FIELDS):
        pass
    elif set(value) == set(QUALIFICATION_EXTENDED_FIELDS):
        for field in ("style_pack_asset_id", "sealed_case_asset_id", "rubric_asset_id"):
            _identifier(value[field], field)
        for field in ("style_pack_asset_hash", "sealed_case_asset_hash", "rubric_asset_hash"):
            _hash(value[field], field)
        ids = value["writer_output_asset_ids"]
        hashes = value["writer_output_asset_hashes"]
        if (not isinstance(ids, list) or not isinstance(hashes, list)
                or len(ids) != len(hashes) or len(ids) != 3
                or len(set(ids)) != 3 or len(set(hashes)) != 3):
            raise StyleContractError("writer output Asset bindings are not complete")
        for item in ids: _identifier(item, "writer_output_asset_id")
        for item in hashes: _hash(item, "writer_output_asset_hash")
        _identifier(value["review_output_asset_id"], "review_output_asset_id")
        _hash(value["review_output_asset_hash"], "review_output_asset_hash")
    else:
        raise StyleContractError("qualification receipt fields are not closed")
    receipt = value
    if receipt["schema"] != "author-style-qualification-receipt/v1":
        raise StyleContractError("qualification receipt schema mismatch")
    for field in ("receipt_id", "style_pack_id", "manufacturing_route_id", "writer_route_id", "reviewer_route_id"):
        _identifier(receipt[field], field)
    for field in ("style_release_id", "style_payload_hash", "qualification_run_snapshot_hash", "rubric_hash", "receipt_hash"):
        _hash(receipt[field], field)
    routes = [receipt["manufacturing_route_id"], receipt["writer_route_id"], receipt["reviewer_route_id"]]
    if len(set(routes)) != 3:
        raise StyleContractError("qualification routes must be pairwise independent")
    attempt_sets: list[set[str]] = []
    for field in ("manufacturing_attempt_ids", "writer_attempt_ids", "reviewer_attempt_ids"):
        attempts = _strings(receipt[field], field)
        if not attempts:
            raise StyleContractError(f"{field} must be non-empty")
        for item in attempts:
            _identifier(item, field)
        attempt_sets.append(set(attempts))
    if any(attempt_sets[a] & attempt_sets[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise StyleContractError("qualification attempt paths overlap")
    cases = receipt["case_results"]
    if not isinstance(cases, list) or len(cases) != 3:
        raise StyleContractError("qualification requires exactly three cases")
    case_ids: list[str] = []
    for index, raw in enumerate(cases):
        case = _exact(raw, CASE_FIELDS, f"case_results[{index}]")
        case_ids.append(_identifier(case["case_id"], "case_id"))
        _hash(case["writer_output_hash"], "writer_output_hash")
        _hash(case["review_output_hash"], "review_output_hash")
        score = case["score_0_100"]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise StyleContractError("score_0_100 must be an integer in [0,100]")
        if not isinstance(case["passed"], bool):
            raise StyleContractError("case passed must be boolean")
    if len(case_ids) != len(set(case_ids)):
        raise StyleContractError("qualification case IDs must be unique")
    all_passed = all(case["passed"] for case in cases)
    if receipt["decision"] not in {"pass", "fail"} or (receipt["decision"] == "pass") != all_passed:
        raise StyleContractError("qualification decision does not match cases")
    if not isinstance(receipt["automatic_eligible"], bool) or receipt["automatic_eligible"] != all_passed:
        raise StyleContractError("automatic_eligible does not match qualification decision")
    projected = deepcopy(receipt)
    projected.pop("receipt_hash")
    if receipt["receipt_hash"] != hash_jcs("author-style-qualification-receipt/v1", projected):
        raise StyleContractError("qualification receipt_hash mismatch")
    if receipt["style_pack_id"] != pack["style_pack_id"]:
        raise StyleContractError("qualification style_pack_id mismatch")
    if receipt["style_release_id"] != pack["style_release_id"]:
        raise StyleContractError("qualification is not for the exact style release")
    if receipt["style_payload_hash"] != pack["payload_hash"]:
        raise StyleContractError("qualification is not bound to the style payload")
    if receipt["manufacturing_route_id"] != pack["manufacture_snapshot"]["route_id"]:
        raise StyleContractError("qualification manufacturing route is not pack-bound")
    if not set(pack["manufacture_snapshot"]["provider_attempt_ids"]).issubset(attempt_sets[0]):
        raise StyleContractError("qualification manufacturing attempts do not cover the pack")
    if receipt["decision"] != "pass" or receipt["automatic_eligible"] is not True:
        raise StyleContractError("exact style release is not eligible")
    if set(receipt) == set(QUALIFICATION_EXTENDED_FIELDS):
        if receipt["style_pack_asset_hash"] != sha256_bytes(canonical_bytes(pack)):
            raise StyleContractError("qualification style pack Asset hash is not exact")
    return deepcopy(receipt)


def validate_qualified_style(pack: Mapping[str, Any], receipt: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    checked_pack = validate_style_pack(pack)
    checked_receipt = validate_qualification_receipt(receipt, pack=checked_pack)
    return checked_pack, checked_receipt


def validate_lexicon(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = frozenset({"schema", "lexicon_id", "version", "normalization", "conflict_policy", "entries"})
    checked = _exact(value, fields, "lexicon/v1")
    if (checked["schema"] != "lexicon/v1"
            or checked["normalization"] != "unicode-nfc-casefold-v1"
            or checked["conflict_policy"] != "highest-priority-then-plugin-id-byte-order"):
        raise StyleContractError("lexicon schema/policy mismatch")
    _identifier(checked["lexicon_id"], "lexicon_id")
    if not isinstance(checked["version"], str) or SEMVER_RE.fullmatch(checked["version"]) is None:
        raise StyleContractError("lexicon version must be SemVer")
    entries = checked["entries"]
    if not isinstance(entries, list): raise StyleContractError("lexicon entries must be an array")
    keys: list[str] = []
    row_fields = frozenset({"term", "normalized_term", "preferred", "aliases", "forbidden", "note", "priority", "source_plugin_id"})
    for raw in entries:
        row = _exact(raw, row_fields, "lexicon entry")
        for field in ("term", "normalized_term", "preferred", "note"):
            if not isinstance(row[field], str) or (field != "note" and not row[field]):
                raise StyleContractError(f"lexicon {field} invalid")
        aliases = _strings(row["aliases"], "lexicon aliases")
        if aliases != sorted(set(aliases), key=lambda item: item.encode("utf-8")):
            raise StyleContractError("lexicon aliases must be UTF-8 byte-sorted")
        if not isinstance(row["forbidden"], bool) or isinstance(row["priority"], bool) or not isinstance(row["priority"], int):
            raise StyleContractError("lexicon flag/priority invalid")
        _identifier(row["source_plugin_id"], "lexicon source_plugin_id")
        keys.append(row["normalized_term"])
    if keys != sorted(set(keys), key=lambda item: item.encode("utf-8")):
        raise StyleContractError("lexicon entries must be normalized-key sorted unique")
    return deepcopy(checked)


def merge_lexicons(values: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Deterministically merge exact lexicon releases without treating them as Style or Skill."""
    lexicon_fields = frozenset({"schema", "lexicon_id", "version", "normalization", "conflict_policy", "entries"})
    entry_fields = frozenset({"term", "normalized_term", "preferred", "aliases", "forbidden", "note", "priority", "source_plugin_id"})
    entries: dict[str, list[dict[str, Any]]] = {}
    sources: list[dict[str, str]] = []
    for ordinal, raw in enumerate(values):
        value = validate_lexicon(raw)
        sources.append({"lexicon_id": value["lexicon_id"], "version": value["version"]})
        observed: list[str] = []
        for item in value["entries"]:
            row = _exact(item, entry_fields, "lexicon entry")
            for field in ("term", "normalized_term", "preferred", "note"):
                if not isinstance(row[field], str) or (field in {"term", "normalized_term"} and not row[field]):
                    raise StyleContractError(f"lexicon {field} is invalid")
            aliases = _strings(row["aliases"], "lexicon aliases")
            if aliases != sorted(aliases, key=lambda child: child.encode("utf-8")):
                raise StyleContractError("lexicon aliases must be UTF-8 byte-sorted")
            if not isinstance(row["forbidden"], bool):
                raise StyleContractError("lexicon forbidden must be boolean")
            if isinstance(row["priority"], bool) or not isinstance(row["priority"], int):
                raise StyleContractError("lexicon priority must be integer")
            _identifier(row["source_plugin_id"], "lexicon source_plugin_id")
            observed.append(row["normalized_term"])
            entries.setdefault(row["normalized_term"], []).append(deepcopy(row))
        if observed != sorted(observed, key=lambda child: child.encode("utf-8")) or len(observed) != len(set(observed)):
            raise StyleContractError("lexicon entries must be unique and UTF-8 byte-sorted by normalized_term")

    winners: list[dict[str, Any]] = []
    for normalized_term in sorted(entries, key=lambda child: child.encode("utf-8")):
        rows = entries[normalized_term]
        rows.sort(key=lambda row: (
            -row["priority"],
            row["source_plugin_id"].encode("utf-8"),
            canonical_bytes(row),
        ))
        winners.append(rows[0])
    return {
        "schema": "style-runtime-lexicon-merge/v1",
        "normalization": "unicode-nfc-casefold-v1",
        "conflict_policy": "highest-priority-then-plugin-id-byte-order",
        "sources": sources,
        "entries": winners,
    }
