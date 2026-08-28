"""Closed, dependency-light contracts for the NAP-01 source cleaner.

The module intentionally has no dependency on PlotPilot Core, a database, or a
filesystem-backed asset store.  Core supplies immutable Asset IDs and the
RunSnapshot; the worker only validates those bindings and returns a result
Bundle.  The JSON representation used here is the RFC 8785-compatible subset
needed by the frozen source contracts (UTF-8 strings, integers, booleans,
arrays and objects).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _sdk_canonical_bytes
    from plotpilot_plugin_sdk.canonical import hash_jcs as _sdk_hash_jcs
    from plotpilot_plugin_sdk.canonical import sha256_hex as _sdk_sha256_hex
except ImportError as exc:  # pragma: no cover - exercised by the fail-closed runtime test
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _sdk_canonical_bytes = None
    _sdk_hash_jcs = None
    _sdk_sha256_hex = None
else:
    _SDK_IMPORT_ERROR = None

SOURCE_CLEANING_RULES_SCHEMA = "source-cleaning-rules/v1"
SOURCE_CLEANING_PROFILE_SCHEMA = "source-cleaning-profile/v1"
SOURCE_CLEANING_MATCH_MANIFEST_SCHEMA = "source-cleaning-match-manifest/v1"
SOURCE_CLEANING_RECEIPT_SCHEMA = "source-cleaning-receipt/v1"
SOURCE_CLEANING_PREVIEW_ARTIFACT_SCHEMA = "source.clean.preview-artifact/v1"
SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA = "core/document-text/v1"
SOURCE_CLEANING_MERGE_ARTIFACT_SCHEMA = "source.clean.rules.merge-artifact/v1"

CORE_REGEX_ENGINE = "core_regex/v1"
CORE_REGEX_PACKAGE = "regex"
CORE_REGEX_VERSION = "2026.7.19"
CORE_REGEX_SYNTAX_VERSION = "regex.VERSION1"
CORE_REGEX_WHEEL = "regex-2026.7.19-cp312-cp312-win_amd64.whl"
CORE_REGEX_SOURCE_COMMIT = "0525affc5309f8d53deac351e4926b11eb8a282c"
CORE_REGEX_LICENSE = "Apache-2.0 AND CNRI-Python"
CORE_REGEX_WHEEL_SHA256 = "e30d40268a28d54ce0437031750497004c22602b8e3ab891f759b795a003b312"
REGEX_RUNTIME_BLOCKER = (
    "requires regex==2026.7.19 with regex.VERSION1 and native timeout; "
    "re/thread fallback is forbidden"
)

RULE_SCOPES = ("line", "paragraph", "chapter")
RULE_TARGETS = ("body", "title")
INPUT_KINDS = ("txt", "epub", "paste", "all")
REGEX_FLAG_ORDER = ("ignore_case", "multiline", "dotall")

CAPABILITY_PREVIEW = "source.clean.preview/v1"
CAPABILITY_APPLY = "source.clean.apply/v1"
CAPABILITY_MERGE = "source.clean.rules.merge/v1"

DEFAULT_LIMITS: dict[str, int] = {
    "scope_timeout_ms": 100,
    "rule_timeout_ms": 1_000,
    "import_timeout_ms": 10_000,
    "max_scope_codepoints": 100_000,
    "max_rule_scanned_codepoints": 1_000_000,
    "max_import_scanned_codepoints": 10_000_000,
    "max_scope_matches": 10_000,
    "max_rule_matches": 100_000,
    "max_import_matches": 1_000_000,
    "max_plugins": 16,
    "max_rules": 128,
    "max_pattern_bytes": 32_768,
    "max_snapshot_bytes": 8_388_608,
    "max_persisted_matches": 1_000_000,
}

_RULE_KEYS = {
    "id",
    "name",
    "description",
    "pattern",
    "scope",
    "target",
    "input_kinds",
    "flags",
    "enabled",
    "order",
    "tags",
}
_PROFILE_KEYS = {
    "schema",
    "rules_release_id",
    "rules_package_hash",
    "rules_hash",
    "rule_order",
    "thresholds",
    "excluded_hit_ids",
    "profile_hash",
}
_THRESHOLD_KEYS = {"max_total_matches", "max_deleted_codepoints"}


class ContractError(ValueError):
    """A closed contract or hash binding is invalid."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RegexRuntimeUnavailable(ContractError):
    """The frozen core_regex runtime cannot be proven available."""

    def __init__(self, detail: str = "runtime import/version/syntax/timeout check failed"):
        super().__init__("REGEX_RUNTIME_UNAVAILABLE", f"{REGEX_RUNTIME_BLOCKER}; {detail}")


class StaleRevisionError(ContractError):
    """The caller attempted to operate on a revision other than the frozen one."""

    def __init__(self, message: str = "request revision/text binding is stale"):
        super().__init__("STALE_REVISION", message)


class HostBindingError(ContractError):
    """The injected Core host did not return a verified immutable Asset."""

    def __init__(self, message: str):
        super().__init__("HOST_ASSET_BINDING_INVALID", message)


def _require_public_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None or _sdk_canonical_bytes is None or _sdk_hash_jcs is None or _sdk_sha256_hex is None:
        raise ContractError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; cleaning is fail-closed") from _SDK_IMPORT_ERROR


def _reject_surrogates(value: Any, path: str = "value") -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ContractError("UNICODE_SCALAR_REQUIRED", f"{path} contains a lone surrogate") from exc
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON_OBJECT_KEY_INVALID", f"{path} has a non-string key")
            _reject_surrogates(key, f"{path}.<key>")
            _reject_surrogates(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        for index, child in enumerate(value):
            _reject_surrogates(child, f"{path}[{index}]")
    elif isinstance(value, float):
        raise ContractError("JSON_NUMBER_INVALID", f"{path} must not contain floating point values")


def canonical_bytes(value: Any) -> bytes:
    """Delegate canonicalization to the public PlotPilot SDK."""
    _require_public_sdk()
    _reject_surrogates(value)
    try:
        assert _sdk_canonical_bytes is not None
        return bytes(_sdk_canonical_bytes(value))
    except Exception as exc:
        raise ContractError("CANONICAL_JSON_INVALID", "value is not canonical JSON data") from exc


def sha256_bytes(raw: bytes) -> str:
    _require_public_sdk()
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ContractError("BYTES_REQUIRED", "raw must be bytes")
    assert _sdk_sha256_hex is not None
    return str(_sdk_sha256_hex(bytes(raw)))


def sha256_text(text: str) -> str:
    if not isinstance(text, str):
        raise ContractError("TEXT_REQUIRED", "text must be a Unicode string")
    _reject_surrogates(text, "text")
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any, *, domain: str | None = None) -> str:
    _require_public_sdk()
    if domain is None:
        return sha256_bytes(canonical_bytes(value))
    assert _sdk_hash_jcs is not None
    payload = value if isinstance(value, Mapping) and value.get("schema") == domain else {"schema": domain, "payload": value}
    return str(_sdk_hash_jcs(domain, payload))


def require_hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError("HASH_INVALID", f"{path} must be a lowercase SHA-256")
    return value


def require_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ContractError("ID_INVALID", f"{path} must be a non-empty bounded string")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-"
    if value[0] not in allowed or any(char not in allowed for char in value):
        raise ContractError("ID_INVALID", f"{path} contains unsupported characters")
    return value


def _closed(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("OBJECT_REQUIRED", f"{path} must be an object")
    actual = set(value)
    missing = sorted(keys - actual)
    extra = sorted(actual - keys)
    if missing:
        raise ContractError("FIELD_MISSING", f"{path} missing {','.join(missing)}")
    if extra:
        raise ContractError("UNKNOWN_FIELD", f"{path} contains {','.join(extra)}")
    return dict(value)


def _string(value: Any, path: str, *, maximum: int = 4096, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value):
        raise ContractError("STRING_INVALID", f"{path} must be a bounded Unicode string")
    _reject_surrogates(value, path)
    return value


def _integer(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("INTEGER_INVALID", f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError("INTEGER_INVALID", f"{path} is below {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError("INTEGER_INVALID", f"{path} is above {maximum}")
    return value


def validate_limits(value: Mapping[str, Any] | None) -> dict[str, int]:
    data = deepcopy(DEFAULT_LIMITS if value is None else dict(value))
    if set(data) != set(DEFAULT_LIMITS):
        raise ContractError("LIMIT_POLICY_INVALID", "limits must use the frozen closed key set")
    for key, raw in data.items():
        data[key] = _integer(raw, f"limits.{key}", minimum=1)
    for left, right in (
        ("scope_timeout_ms", "rule_timeout_ms"),
        ("rule_timeout_ms", "import_timeout_ms"),
        ("max_scope_codepoints", "max_rule_scanned_codepoints"),
        ("max_rule_scanned_codepoints", "max_import_scanned_codepoints"),
        ("max_scope_matches", "max_rule_matches"),
        ("max_rule_matches", "max_import_matches"),
    ):
        if data[left] > data[right]:
            raise ContractError("LIMIT_POLICY_INVALID", f"{left} cannot exceed {right}")
    if data["max_persisted_matches"] > data["max_import_matches"]:
        raise ContractError("LIMIT_POLICY_INVALID", "max_persisted_matches cannot exceed max_import_matches")
    return data


def validate_rules(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed(value, {"schema", "rules"}, "rules")
    if data["schema"] != SOURCE_CLEANING_RULES_SCHEMA:
        raise ContractError("RULES_SCHEMA_INVALID", "rules.schema is not source-cleaning-rules/v1")
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ContractError("RULES_INVALID", "rules.rules must be a non-empty array")
    if len(raw_rules) > DEFAULT_LIMITS["max_rules"]:
        raise ContractError("LIMIT_EXCEEDED", "rule count exceeds max_rules")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    orders: set[int] = set()
    for index, raw in enumerate(raw_rules):
        rule = _closed(raw, _RULE_KEYS, f"rules.rules[{index}]")
        rule_id = _string(rule["id"], f"rules.rules[{index}].id", maximum=128)
        if rule_id in ids:
            raise ContractError("RULE_ID_DUPLICATE", f"duplicate rule id: {rule_id}")
        ids.add(rule_id)
        order = _integer(rule["order"], f"rules.rules[{index}].order", minimum=0, maximum=2**31 - 1)
        if order in orders:
            raise ContractError("RULE_ORDER_DUPLICATE", f"duplicate rule order: {order}")
        orders.add(order)
        pattern = _string(rule["pattern"], f"rules.rules[{index}].pattern", maximum=DEFAULT_LIMITS["max_pattern_bytes"])
        if len(pattern.encode("utf-8")) > DEFAULT_LIMITS["max_pattern_bytes"]:
            raise ContractError("LIMIT_EXCEEDED", f"pattern is larger than max_pattern_bytes: {rule_id}")
        name = _string(rule["name"], f"rules.rules[{index}].name", maximum=512)
        description = _string(rule["description"], f"rules.rules[{index}].description", maximum=4096, nonempty=False)
        scope = rule["scope"]
        target = rule["target"]
        if scope not in RULE_SCOPES or target not in RULE_TARGETS:
            raise ContractError("RULE_FIELD_INVALID", f"unsupported scope/target in rule: {rule_id}")
        kinds = rule["input_kinds"]
        if not isinstance(kinds, list) or not kinds or any(kind not in INPUT_KINDS for kind in kinds):
            raise ContractError("RULE_FIELD_INVALID", f"input_kinds invalid in rule: {rule_id}")
        if len(kinds) != len(set(kinds)):
            raise ContractError("RULE_FIELD_INVALID", f"input_kinds duplicate in rule: {rule_id}")
        if "all" in kinds and kinds != ["all"]:
            raise ContractError("RULE_FIELD_INVALID", f"all must be the only input kind in rule: {rule_id}")
        expected_kinds = [kind for kind in INPUT_KINDS if kind != "all"]
        if kinds != ["all"] and kinds != [kind for kind in expected_kinds if kind in kinds]:
            raise ContractError("RULE_FIELD_INVALID", f"input_kinds must use frozen order in rule: {rule_id}")
        flags = rule["flags"]
        if not isinstance(flags, list) or any(flag not in REGEX_FLAG_ORDER for flag in flags):
            raise ContractError("RULE_FIELD_INVALID", f"flags invalid in rule: {rule_id}")
        if len(flags) != len(set(flags)) or flags != [flag for flag in REGEX_FLAG_ORDER if flag in flags]:
            raise ContractError("RULE_FIELD_INVALID", f"flags must use frozen order in rule: {rule_id}")
        if not isinstance(rule["enabled"], bool):
            raise ContractError("RULE_FIELD_INVALID", f"enabled must be boolean in rule: {rule_id}")
        tags = rule["tags"]
        if not isinstance(tags, list) or len(tags) != len(set(tags)) or any(not isinstance(tag, str) for tag in tags):
            raise ContractError("RULE_FIELD_INVALID", f"tags invalid in rule: {rule_id}")
        for tag in tags:
            _string(tag, f"rules.rules[{index}].tags", maximum=128)
        result.append(
            {
                "id": rule_id,
                "name": name,
                "description": description,
                "pattern": pattern,
                "scope": scope,
                "target": target,
                "input_kinds": list(kinds),
                "flags": list(flags),
                "enabled": rule["enabled"],
                "order": order,
                "tags": list(tags),
            }
        )
    result.sort(key=lambda child: (child["order"], child["id"]))
    return {"schema": SOURCE_CLEANING_RULES_SCHEMA, "rules": result}


def rules_hash(value: Mapping[str, Any]) -> str:
    normalized = validate_rules(value)
    return sha256_json(normalized, domain=SOURCE_CLEANING_RULES_SCHEMA)


def build_profile(
    rules: Mapping[str, Any],
    *,
    rules_release_id: str,
    rules_package_hash: str,
    thresholds: Mapping[str, Any] | None = None,
    excluded_hit_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    normalized_rules = validate_rules(rules)
    release_id = require_hash(rules_release_id, "rules_release_id")
    package_hash = require_hash(rules_package_hash, "rules_package_hash")
    threshold_data = dict(thresholds or {"max_total_matches": 100_000, "max_deleted_codepoints": 10_000_000})
    if set(threshold_data) != _THRESHOLD_KEYS:
        raise ContractError("THRESHOLD_INVALID", "thresholds use an unsupported key set")
    for key, raw in threshold_data.items():
        _integer(raw, f"thresholds.{key}", minimum=0)
    excluded = sorted(set(excluded_hit_ids or []))
    if len(excluded) != len(list(excluded_hit_ids or [])):
        raise ContractError("EXCEPTION_INVALID", "excluded_hit_ids must be unique")
    for hit_id in excluded:
        require_hash(hit_id, "excluded_hit_ids")
    body = {
        "schema": SOURCE_CLEANING_PROFILE_SCHEMA,
        "rules_release_id": release_id,
        "rules_package_hash": package_hash,
        "rules_hash": rules_hash(normalized_rules),
        "rule_order": [rule["id"] for rule in normalized_rules["rules"]],
        "thresholds": threshold_data,
        "excluded_hit_ids": excluded,
    }
    body["profile_hash"] = sha256_json(body, domain=SOURCE_CLEANING_PROFILE_SCHEMA)
    return body


def validate_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed(value, _PROFILE_KEYS, "profile")
    if data["schema"] != SOURCE_CLEANING_PROFILE_SCHEMA:
        raise ContractError("PROFILE_SCHEMA_INVALID", "profile.schema is unsupported")
    require_hash(data["rules_release_id"], "profile.rules_release_id")
    require_hash(data["rules_package_hash"], "profile.rules_package_hash")
    require_hash(data["rules_hash"], "profile.rules_hash")
    require_hash(data["profile_hash"], "profile.profile_hash")
    order = data["rule_order"]
    if not isinstance(order, list) or not order or len(order) != len(set(order)) or any(not isinstance(item, str) for item in order):
        raise ContractError("PROFILE_INVALID", "profile.rule_order must be a unique ordered array")
    thresholds = data["thresholds"]
    if not isinstance(thresholds, Mapping) or set(thresholds) != _THRESHOLD_KEYS:
        raise ContractError("THRESHOLD_INVALID", "profile.thresholds use an unsupported key set")
    for key, raw in thresholds.items():
        _integer(raw, f"profile.thresholds.{key}", minimum=0)
    excluded = data["excluded_hit_ids"]
    if not isinstance(excluded, list) or excluded != sorted(excluded) or len(excluded) != len(set(excluded)):
        raise ContractError("EXCEPTION_INVALID", "profile.excluded_hit_ids must be sorted and unique")
    for hit_id in excluded:
        require_hash(hit_id, "profile.excluded_hit_ids")
    expected = {key: child for key, child in data.items() if key != "profile_hash"}
    if sha256_json(expected, domain=SOURCE_CLEANING_PROFILE_SCHEMA) != data["profile_hash"]:
        raise ContractError("HASH_BINDING_INVALID", "profile_hash does not bind the profile")
    return deepcopy(data)


def validate_span(value: Mapping[str, Any], *, text_length: int, path: str) -> dict[str, int]:
    span = _closed(value, {"start", "end"}, path)
    start = _integer(span["start"], f"{path}.start", minimum=0, maximum=text_length)
    end = _integer(span["end"], f"{path}.end", minimum=0, maximum=text_length)
    if end < start:
        raise ContractError("BOUNDARY_INVALID", f"{path}.end precedes start")
    return {"start": start, "end": end}


def validate_ranges(value: Any, *, text_length: int, path: str, allow_empty: bool = True) -> list[dict[str, int]]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ContractError("BOUNDARY_INVALID", f"{path} must be an array")
    result: list[dict[str, int]] = []
    previous_end = -1
    for index, raw in enumerate(value):
        span = validate_span(raw, text_length=text_length, path=f"{path}[{index}]")
        if span["start"] < previous_end:
            raise ContractError("BOUNDARY_INVALID", f"{path} ranges overlap or are out of order")
        result.append(span)
        previous_end = span["end"]
    return result


def validate_match_row(value: Mapping[str, Any], *, text_length: int, path: str = "match_row") -> dict[str, Any]:
    keys = {
        "hit_id",
        "rule_id",
        "rule_order",
        "scope",
        "target",
        "start_codepoint",
        "end_codepoint",
        "quote",
        "quote_hash",
        "excluded",
    }
    row = _closed(value, keys, path)
    require_hash(row["hit_id"], f"{path}.hit_id")
    _string(row["rule_id"], f"{path}.rule_id", maximum=128)
    _integer(row["rule_order"], f"{path}.rule_order", minimum=0)
    if row["scope"] not in RULE_SCOPES or row["target"] not in RULE_TARGETS:
        raise ContractError("MATCH_ROW_INVALID", f"{path} has unsupported scope/target")
    start = _integer(row["start_codepoint"], f"{path}.start_codepoint", minimum=0, maximum=text_length)
    end = _integer(row["end_codepoint"], f"{path}.end_codepoint", minimum=0, maximum=text_length)
    if end <= start:
        raise ContractError("EMPTY_MATCH", f"{path} must be non-empty")
    quote = _string(row["quote"], f"{path}.quote", maximum=text_length + 1)
    if len(quote) != end - start:
        raise ContractError("MATCH_ROW_INVALID", f"{path}.quote length does not match its span")
    if row["quote_hash"] != sha256_text(quote):
        raise ContractError("HASH_BINDING_INVALID", f"{path}.quote_hash does not bind quote")
    if not isinstance(row["excluded"], bool):
        raise ContractError("MATCH_ROW_INVALID", f"{path}.excluded must be boolean")
    return deepcopy(row)


def validate_evidence_span(
    value: Mapping[str, Any],
    *,
    canonical_text: str,
    expected_workspace_id: str | None = None,
    expected_document_id: str | None = None,
    expected_revision_id: str | None = None,
    node_ranges: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    keys = {
        "schema",
        "workspace_id",
        "document_id",
        "revision_id",
        "node_id",
        "start_codepoint",
        "end_codepoint",
        "quote",
        "quote_hash",
        "canonical_text_hash",
    }
    span = _closed(value, keys, "evidence_span")
    if span["schema"] != "evidence-span/v1":
        raise ContractError("EVIDENCE_SPAN_INVALID", "unsupported evidence span schema")
    workspace_id = require_id(span["workspace_id"], "evidence_span.workspace_id")
    document_id = require_id(span["document_id"], "evidence_span.document_id")
    revision_id = require_id(span["revision_id"], "evidence_span.revision_id")
    node_id = require_id(span["node_id"], "evidence_span.node_id")
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        raise ContractError("EVIDENCE_SPAN_INVALID", "workspace binding differs")
    if expected_document_id is not None and document_id != expected_document_id:
        raise ContractError("EVIDENCE_SPAN_INVALID", "document binding differs")
    if expected_revision_id is not None and revision_id != expected_revision_id:
        raise ContractError("EVIDENCE_SPAN_INVALID", "revision binding differs")
    start = _integer(span["start_codepoint"], "evidence_span.start_codepoint", minimum=0, maximum=len(canonical_text))
    end = _integer(span["end_codepoint"], "evidence_span.end_codepoint", minimum=0, maximum=len(canonical_text))
    if end <= start:
        raise ContractError("EVIDENCE_SPAN_INVALID", "span must be non-empty")
    quote = _string(span["quote"], "evidence_span.quote", maximum=len(canonical_text))
    if canonical_text[start:end] != quote:
        raise ContractError("EVIDENCE_SPAN_INVALID", "quote is not the exact canonical text slice")
    if span["quote_hash"] != sha256_text(quote):
        raise ContractError("HASH_BINDING_INVALID", "quote_hash is not lowercase SHA-256(UTF-8 quote)")
    if span["canonical_text_hash"] != sha256_text(canonical_text):
        raise ContractError("HASH_BINDING_INVALID", "canonical_text_hash does not bind canonical text")
    if node_ranges is not None:
        node = node_ranges.get(node_id)
        if node is None or not (node["start"] <= start < end <= node["end"]):
            raise ContractError("EVIDENCE_SPAN_INVALID", "span is outside its node range")
    return deepcopy(span)


def build_evidence_span(
    *,
    workspace_id: str,
    document_id: str,
    revision_id: str,
    node_id: str,
    start: int,
    end: int,
    canonical_text: str,
) -> dict[str, Any]:
    if not isinstance(canonical_text, str):
        raise ContractError("TEXT_REQUIRED", "canonical_text must be a Unicode string")
    quote = canonical_text[start:end]
    span = {
        "schema": "evidence-span/v1",
        "workspace_id": workspace_id,
        "document_id": document_id,
        "revision_id": revision_id,
        "node_id": node_id,
        "start_codepoint": start,
        "end_codepoint": end,
        "quote": quote,
        "quote_hash": sha256_text(quote),
        "canonical_text_hash": sha256_text(canonical_text),
    }
    return validate_evidence_span(span, canonical_text=canonical_text)


__all__ = [
    "CAPABILITY_APPLY",
    "CAPABILITY_MERGE",
    "CAPABILITY_PREVIEW",
    "CORE_REGEX_ENGINE",
    "CORE_REGEX_LICENSE",
    "CORE_REGEX_PACKAGE",
    "CORE_REGEX_SOURCE_COMMIT",
    "CORE_REGEX_SYNTAX_VERSION",
    "CORE_REGEX_VERSION",
    "CORE_REGEX_WHEEL",
    "CORE_REGEX_WHEEL_SHA256",
    "ContractError",
    "DEFAULT_LIMITS",
    "HostBindingError",
    "INPUT_KINDS",
    "REGEX_FLAG_ORDER",
    "REGEX_RUNTIME_BLOCKER",
    "RegexRuntimeUnavailable",
    "RULE_SCOPES",
    "RULE_TARGETS",
    "SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA",
    "SOURCE_CLEANING_MATCH_MANIFEST_SCHEMA",
    "SOURCE_CLEANING_MERGE_ARTIFACT_SCHEMA",
    "SOURCE_CLEANING_PREVIEW_ARTIFACT_SCHEMA",
    "SOURCE_CLEANING_RECEIPT_SCHEMA",
    "SOURCE_CLEANING_RULES_SCHEMA",
    "SOURCE_CLEANING_PROFILE_SCHEMA",
    "StaleRevisionError",
    "build_evidence_span",
    "build_profile",
    "canonical_bytes",
    "require_hash",
    "require_id",
    "sha256_bytes",
    "sha256_json",
    "sha256_text",
    "validate_evidence_span",
    "validate_limits",
    "validate_match_row",
    "validate_profile",
    "validate_ranges",
    "validate_rules",
    "rules_hash",
]
