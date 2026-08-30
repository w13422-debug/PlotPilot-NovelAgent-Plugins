"""Deterministic source-cleaning worker for NAP-B1-CLEAN-002A.

The worker is deliberately a pure transformation around two injected host
ports: an immutable input binding supplied in the request and
``host.asset.create`` for output payloads.  It never opens a path, talks to a
database, creates a Revision, or stages a Candidate.  Candidate-shaped output
is produced only by ``source.clean.apply/v1``; rule merge and preview are
artifact-only operations.

The default matcher loader accepts exactly the donor-frozen ``regex`` runtime.
There is no stdlib regular-expression or thread timeout fallback.  Tests may
inject a clearly marked locked engine double, but the production path always
performs the same identity, VERSION1, and native-timeout checks.
"""

from __future__ import annotations

import base64
import importlib
import importlib.metadata
import time
import unicodedata
from _thread import allocate_lock
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

try:
    from plotpilot_plugin_sdk.canonical import (
        canonical_bytes as _sdk_canonical_bytes,
        hash_jcs as _sdk_hash_jcs,
        parse_json_bytes as _sdk_parse_json_bytes,
        sha256_hex as _sdk_sha256_hex,
    )
    from plotpilot_plugin_sdk.verifier import (
        validate_rpc_result as _sdk_validate_rpc_result,
        verify_checkpoint as _sdk_verify_checkpoint,
        verify_provenance_receipt as _sdk_verify_receipt,
        verify_result_bundle as _sdk_verify_result,
    )
except ImportError as exc:  # pragma: no cover - exercised by the fail-closed test
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _sdk_canonical_bytes = None
    _sdk_hash_jcs = None
    _sdk_parse_json_bytes = None
    _sdk_sha256_hex = None
    _sdk_validate_rpc_result = None
    _sdk_verify_checkpoint = None
    _sdk_verify_receipt = None
    _sdk_verify_result = None
else:
    _SDK_IMPORT_ERROR = None

from .contract import (
    CAPABILITY_APPLY,
    CAPABILITY_MERGE,
    CAPABILITY_PREVIEW,
    CORE_REGEX_ENGINE,
    CORE_REGEX_PACKAGE,
    CORE_REGEX_SYNTAX_VERSION,
    CORE_REGEX_VERSION,
    ContractError,
    DEFAULT_LIMITS,
    HostBindingError,
    REGEX_FLAG_ORDER,
    RegexRuntimeUnavailable,
    SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA,
    SOURCE_CLEANING_MATCH_MANIFEST_SCHEMA,
    SOURCE_CLEANING_MERGE_ARTIFACT_SCHEMA,
    SOURCE_CLEANING_PREVIEW_ARTIFACT_SCHEMA,
    SOURCE_CLEANING_RECEIPT_SCHEMA,
    SOURCE_CLEANING_RULES_SCHEMA,
    StaleRevisionError,
    build_profile,
    require_hash,
    require_id,
    rules_hash,
    sha256_text,
    validate_limits,
    validate_match_row,
    validate_profile,
    validate_ranges,
    validate_rules,
)
from .package_identity import PLUGIN_ID, load_identity


_CLEANING_COMMON_KEYS = {
    "schema",
    "operation_key",
    "operation",
    "job_id",
    "step_id",
    "attempt_id",
    "worker_run_id",
    "lease_epoch",
    "checkpoint_ids",
    "provenance_receipt_id",
    "created_at",
    "total_units",
    "run_snapshot_hash",
    "workspace_id",
    "document_id",
    "revision_id",
    "base_content_hash",
    "source_asset_id",
    "source_asset_hash",
    "storage_source_format",
    "input_kind",
    "rules",
    "rules_release_id",
    "rules_package_hash",
    "profile",
    "limits",
    "body_ranges",
    "title_ranges",
}
_RESUME_KEYS = {
    "resume_checkpoint_asset_id",
    "resume_checkpoint_asset_hash",
    "resume_state_asset_id",
    "resume_state_asset_hash",
}
_REQUEST_IDENTITY_EPHEMERAL_KEYS = {"operation", *_RESUME_KEYS}
_REVIEW_KEYS = {
    "schema",
    "run_snapshot_hash",
    "source_asset_hash",
    "source_text_hash",
    "rules_release_id",
    "rules_package_hash",
    "rules_hash",
    "profile_hash",
    "rule_order",
    "match_rows",
    "match_manifest_hash",
    "cleaned_text_hash",
    "canonical_text_hash",
}
_MERGE_KEYS = {
    "schema",
    "operation_key",
    "operation",
    "job_id",
    "step_id",
    "attempt_id",
    "worker_run_id",
    "lease_epoch",
    "checkpoint_ids",
    "provenance_receipt_id",
    "created_at",
    "total_units",
    "run_snapshot_hash",
    "workspace_id",
    "rules_packages",
}
_MERGE_PACKAGE_KEYS = {"package_id", "version", "release_id", "package_hash", "rules"}


def _closed(value: Any, keys: set[str], path: str, *, optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("OBJECT_REQUIRED", f"{path} must be an object")
    optional_keys = set(optional or ())
    allowed_keys = keys | optional_keys
    actual = set(value)
    missing = sorted(keys - actual)
    extra = sorted(actual - allowed_keys)
    if missing:
        raise ContractError("FIELD_MISSING", f"{path} missing {','.join(missing)}")
    if extra:
        raise ContractError("UNKNOWN_FIELD", f"{path} contains {','.join(extra)}")
    return dict(value)


def _string(value: Any, path: str, *, maximum: int = 4096, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value):
        raise ContractError("STRING_INVALID", f"{path} must be a bounded Unicode string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContractError("UNICODE_SCALAR_REQUIRED", f"{path} contains a lone surrogate") from exc
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError("INTEGER_INVALID", f"{path} must be an integer >= {minimum}")
    return value


def _id(value: Any, path: str) -> str:
    return require_id(value, path)


def _hash(value: Any, path: str) -> str:
    return require_hash(value, path)


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        ...


@dataclass
class _RunContext:
    request_hash: str
    checkpoint_ids: tuple[str, ...]
    last_job_event_seq: int = 0
    last_checkpoint_seq: int = 0
    # host.candidate.stage/v1 is a distinct RPC receipt.  Keep its frozen
    # object rows intact; provenance-receipt/v1 receives only their item_id
    # references, as required by the public SDK contract.
    stage_response: dict[str, object] | None = None
    terminal_outcome: str | None = None


class _Cancelled(RuntimeError):
    pass


def _require_sdk() -> None:
    if (
        _SDK_IMPORT_ERROR is not None
        or _sdk_canonical_bytes is None
        or _sdk_hash_jcs is None
        or _sdk_parse_json_bytes is None
        or _sdk_sha256_hex is None
        or _sdk_validate_rpc_result is None
        or _sdk_verify_checkpoint is None
        or _sdk_verify_receipt is None
        or _sdk_verify_result is None
    ):
        raise ContractError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; cleaning is fail-closed") from _SDK_IMPORT_ERROR


def _canonical(value: Any) -> bytes:
    _require_sdk()
    assert _sdk_canonical_bytes is not None
    try:
        return bytes(_sdk_canonical_bytes(value))
    except Exception as exc:
        raise ContractError("CANONICAL_JSON_INVALID", "value is not canonical JSON data") from exc


def _hash_jcs(prefix: str, value: Any) -> str:
    _require_sdk()
    assert _sdk_hash_jcs is not None
    try:
        return str(_sdk_hash_jcs(prefix, value))
    except Exception as exc:
        raise ContractError("CANONICAL_JSON_INVALID", f"cannot hash {prefix}") from exc


def _sha256(raw: bytes) -> str:
    _require_sdk()
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ContractError("BYTES_REQUIRED", "raw must be bytes")
    assert _sdk_sha256_hex is not None
    return str(_sdk_sha256_hex(bytes(raw)))


def _sha256_text(text: str) -> str:
    if not isinstance(text, str):
        raise ContractError("TEXT_REQUIRED", "text must be a Unicode string")
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContractError("UNICODE_SCALAR_REQUIRED", "text contains a lone surrogate") from exc
    return _sha256(raw)


def _verify_result(bundle: Mapping[str, Any], *, workspace_id: str | None, snapshot_hash: str) -> None:
    _require_sdk()
    assert _sdk_verify_result is not None
    try:
        _sdk_verify_result(
            bundle,
            snapshot_workspace_id=workspace_id,
            snapshot_hash_value=snapshot_hash,
        )
    except Exception as exc:
        raise ContractError("RESULT_CONTRACT_INVALID", f"public result verifier rejected the bundle: {exc}") from exc


def _verify_receipt(receipt: Mapping[str, Any]) -> None:
    _require_sdk()
    assert _sdk_verify_receipt is not None
    try:
        _sdk_verify_receipt(receipt)
    except Exception as exc:
        raise ContractError("PROVENANCE_INVALID", f"public provenance verifier rejected the receipt: {exc}") from exc


def _verify_checkpoint(checkpoint: Mapping[str, Any], *, expected_snapshot_hash: str, previous_seq: int | None) -> None:
    _require_sdk()
    assert _sdk_verify_checkpoint is not None
    try:
        _sdk_verify_checkpoint(
            checkpoint,
            expected_snapshot_hash=expected_snapshot_hash,
            previous_seq=previous_seq,
        )
    except Exception as exc:
        raise ContractError("CHECKPOINT_INVALID", f"public checkpoint verifier rejected the checkpoint: {exc}") from exc


def _hash_id(kind: str, value: Any) -> str:
    return f"{kind}-{_hash_jcs('plotpilot-deterministic-id/v1', value)[:32]}"


def _locked_regex_engine(engine: Any | None = None) -> Any:
    """Return only an engine that explicitly proves the frozen contract."""

    if engine is not None:
        if (
            getattr(engine, "engine_id", None) != CORE_REGEX_ENGINE
            or getattr(engine, "package_name", None) != CORE_REGEX_PACKAGE
            or getattr(engine, "dependency_version", None) != CORE_REGEX_VERSION
            or getattr(engine, "syntax_version", None) != CORE_REGEX_SYNTAX_VERSION
            or getattr(engine, "native_timeout", None) is not True
            or not callable(getattr(engine, "compile", None))
        ):
            raise RegexRuntimeUnavailable("injected matcher did not prove the frozen engine contract")
        return engine

    try:
        module = importlib.import_module(CORE_REGEX_PACKAGE)
    except Exception as exc:  # pragma: no cover - environment-dependent branch
        raise RegexRuntimeUnavailable("locked regex module is not importable") from exc
    try:
        distribution_version = importlib.metadata.version(CORE_REGEX_PACKAGE)
    except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover - environment-dependent
        raise RegexRuntimeUnavailable("locked regex distribution metadata is unavailable") from exc
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise RegexRuntimeUnavailable("locked regex distribution metadata could not be read") from exc
    if distribution_version != CORE_REGEX_VERSION:
        raise RegexRuntimeUnavailable(
            f"locked distribution version is {distribution_version!r}, not {CORE_REGEX_VERSION!r}"
        )
    if str(getattr(module, "__version__", distribution_version)) != CORE_REGEX_VERSION:
        raise RegexRuntimeUnavailable("regex module version does not match the locked distribution")
    version1 = getattr(module, "VERSION1", None)
    if version1 is None:
        raise RegexRuntimeUnavailable("regex.VERSION1 is unavailable")
    try:
        probe = module.compile("a", version1)
        # This proves timeout keyword support, not scheduling performance.
        # Keep the actual per-rule timeout below unchanged.
        probe.search("a", timeout=_REGEX_CAPABILITY_PROBE_TIMEOUT_SECONDS)
    except TypeError as exc:
        raise RegexRuntimeUnavailable("regex native timeout keyword is unavailable") from exc
    except Exception as exc:  # pragma: no cover - environment-dependent branch
        raise RegexRuntimeUnavailable("regex VERSION1/native-timeout probe failed") from exc

    class _ExactRuntime:
        engine_id = CORE_REGEX_ENGINE
        package_name = CORE_REGEX_PACKAGE
        dependency_version = CORE_REGEX_VERSION
        syntax_version = CORE_REGEX_SYNTAX_VERSION
        native_timeout = True

        def compile(self, pattern: str, *, flags: Any = 0) -> Any:
            return module.compile(pattern, flags)

        def flags_for(self, names: Sequence[str]) -> Any:
            flags = 0
            for name in names:
                flags |= getattr(module, {"ignore_case": "IGNORECASE", "multiline": "MULTILINE", "dotall": "DOTALL"}[name])
            return flags

    return _ExactRuntime()


def regex_runtime_status() -> dict[str, Any]:
    """Report the exact runtime status without ever selecting a fallback."""

    try:
        _locked_regex_engine()
    except RegexRuntimeUnavailable as exc:
        return {
            "engine": CORE_REGEX_ENGINE,
            "package": CORE_REGEX_PACKAGE,
            "version": CORE_REGEX_VERSION,
            "syntax_version": CORE_REGEX_SYNTAX_VERSION,
            "native_timeout": False,
            "available": False,
            "blocker": str(exc),
        }
    return {
        "engine": CORE_REGEX_ENGINE,
        "package": CORE_REGEX_PACKAGE,
        "version": CORE_REGEX_VERSION,
        "syntax_version": CORE_REGEX_SYNTAX_VERSION,
        "native_timeout": True,
        "available": True,
        "blocker": None,
    }


_FIXED_TIME = "2026-08-27T00:00:00Z"
_ASSET_READ_LENGTH = 8_388_608
_MAX_ASSET_READ_PAGES = 4096
_REGEX_CAPABILITY_PROBE_TIMEOUT_SECONDS = 1.0


def _request_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    """Bind stable request identity, excluding lifecycle-only resume controls."""

    return {
        key: deepcopy(value)
        for key, value in request.items()
        if key not in _REQUEST_IDENTITY_EPHEMERAL_KEYS
    }


def _context_for_request(request: Mapping[str, Any], *, capability: str) -> _RunContext:
    if not isinstance(request, Mapping):
        raise ContractError("OBJECT_REQUIRED", "request must be an object")
    if request.get("operation_key") != capability:
        raise ContractError("OPERATION_INVALID", f"request.operation_key must be {capability}")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id"):
        _id(request.get(field), f"request.{field}")
    _integer(request.get("lease_epoch"), "request.lease_epoch", minimum=1)
    checkpoints = request.get("checkpoint_ids")
    if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) != len(set(checkpoints)):
        raise ContractError("CHECKPOINT_INVALID", "request.checkpoint_ids must be a non-empty unique array")
    for index, checkpoint_id in enumerate(checkpoints):
        _id(checkpoint_id, f"request.checkpoint_ids[{index}]")
    _id(request.get("provenance_receipt_id"), "request.provenance_receipt_id")
    _string(request.get("created_at"), "request.created_at", maximum=64)
    _integer(request.get("total_units"), "request.total_units", minimum=0)
    request_hash = _hash_jcs("source-cleaning-operation/v1", _request_projection(request))
    return _RunContext(request_hash=request_hash, checkpoint_ids=tuple(str(item) for item in checkpoints))


def _validate_context(request: Mapping[str, Any], host: HostPort, *, capability: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    schema = f"{capability.replace('/v1', '')}-request/v1"
    required_keys = set(_CLEANING_COMMON_KEYS)
    if capability == CAPABILITY_APPLY:
        required_keys.add("review_context")
    optional = _RESUME_KEYS if capability == CAPABILITY_APPLY else set()
    data = _closed(request, required_keys, "request", optional=optional)
    if data["schema"] != schema:
        raise ContractError("REQUEST_SCHEMA_INVALID", f"request.schema must be {schema}")
    allowed_operations = {
        CAPABILITY_PREVIEW: {"run"},
        CAPABILITY_APPLY: {"run", "resume"},
    }
    if data["operation_key"] != capability or data["operation"] not in allowed_operations[capability]:
        raise ContractError("OPERATION_INVALID", f"{capability} operation is not executable")
    resume_fields_present = _RESUME_KEYS.intersection(data)
    if capability != CAPABILITY_APPLY and resume_fields_present:
        raise ContractError("RESUME_FIELDS_INVALID", "resume Asset bindings are only valid for apply")
    if data["operation"] == "resume":
        if capability != CAPABILITY_APPLY or resume_fields_present != _RESUME_KEYS:
            raise ContractError("RESUME_FIELDS_INVALID", "resume requires checkpoint/state Asset IDs and hashes")
        for field in ("resume_checkpoint_asset_id", "resume_state_asset_id"):
            _id(data[field], f"request.{field}")
        for field in ("resume_checkpoint_asset_hash", "resume_state_asset_hash"):
            _hash(data[field], f"request.{field}")
    elif resume_fields_present:
        raise ContractError("RESUME_FIELDS_INVALID", "resume Asset bindings require operation=resume")
    _context_for_request(data, capability=capability)
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "workspace_id", "document_id", "revision_id", "source_asset_id"):
        _id(data[field], f"request.{field}")
    _integer(data["lease_epoch"], "request.lease_epoch", minimum=1)
    _hash(data["run_snapshot_hash"], "request.run_snapshot_hash")
    _id(data["provenance_receipt_id"], "request.provenance_receipt_id")
    _string(data["created_at"], "request.created_at", maximum=64)
    _integer(data["total_units"], "request.total_units", minimum=0)
    if data["storage_source_format"] not in {"txt", "plain_text", "epub", "paste"}:
        raise ContractError("INPUT_KIND_INVALID", "unsupported storage_source_format")
    if data["input_kind"] not in {"txt", "epub", "paste"}:
        raise ContractError("INPUT_KIND_INVALID", "unsupported input_kind")
    source_hash = _hash(data["source_asset_hash"], "request.source_asset_hash")
    _hash(data["base_content_hash"], "request.base_content_hash")
    limits = validate_limits(data["limits"])
    source_bytes = _read_asset(
        host,
        data["source_asset_id"],
        source_hash,
        max_bytes=limits["max_snapshot_bytes"],
    )
    try:
        text = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError("SOURCE_ENCODING_INVALID", "source Asset must be strict UTF-8 text") from exc
    if _sha256(source_bytes) != source_hash or _sha256_text(text) != source_hash:
        raise ContractError("HASH_BINDING_INVALID", "source_asset_hash does not bind the complete source Asset")
    data["source_text"] = text
    normalized_rules = validate_rules(data["rules"])
    release_id = _hash(data["rules_release_id"], "request.rules_release_id")
    package_hash = _hash(data["rules_package_hash"], "request.rules_package_hash")
    profile = validate_profile(data["profile"])
    expected_profile = build_profile(
        normalized_rules,
        rules_release_id=release_id,
        rules_package_hash=package_hash,
        thresholds=profile["thresholds"],
        excluded_hit_ids=profile["excluded_hit_ids"],
    )
    if profile != expected_profile:
        raise ContractError("HASH_BINDING_INVALID", "profile does not bind rules, release, package, and exclusions")
    body_ranges = validate_ranges(data["body_ranges"], text_length=len(text), path="request.body_ranges")
    title_ranges = validate_ranges(data["title_ranges"], text_length=len(text), path="request.title_ranges")
    if not body_ranges:
        body_ranges = [{"start": 0, "end": len(text)}]
    if len(text.encode("utf-8")) > limits["max_snapshot_bytes"]:
        raise ContractError("LIMIT_EXCEEDED", "source text exceeds max_snapshot_bytes")
    return data, normalized_rules, limits


def _line_ranges(text: str, start: int, end: int) -> list[dict[str, int]]:
    ranges: list[dict[str, int]] = []
    cursor = start
    index = start
    while index < end:
        character = text[index]
        if character in "\r\n":
            if cursor < index:
                ranges.append({"start": cursor, "end": index})
            if character == "\r" and index + 1 < end and text[index + 1] == "\n":
                index += 2
            else:
                index += 1
            cursor = index
            continue
        index += 1
    if cursor < end:
        ranges.append({"start": cursor, "end": end})
    return ranges


def _paragraph_ranges(text: str, start: int, end: int) -> list[dict[str, int]]:
    lines = _line_ranges(text, start, end)
    if not lines:
        return []
    paragraphs: list[dict[str, int]] = []
    current_start: int | None = None
    current_end: int | None = None
    for line in lines:
        if text[line["start"] : line["end"]].strip() == "":
            if current_start is not None and current_end is not None:
                paragraphs.append({"start": current_start, "end": current_end})
            current_start = None
            current_end = None
            continue
        if current_start is None:
            current_start = line["start"]
        current_end = line["end"]
    if current_start is not None and current_end is not None:
        paragraphs.append({"start": current_start, "end": current_end})
    return paragraphs


def _scope_units(
    text: str,
    *,
    scope: str,
    target: str,
    body_ranges: Sequence[Mapping[str, int]],
    title_ranges: Sequence[Mapping[str, int]],
) -> list[dict[str, Any]]:
    target_ranges = title_ranges if target == "title" else body_ranges
    if target == "title" and not target_ranges:
        raise ContractError("TITLE_SCOPE_REQUIRED", "a title-target rule requires frozen title_ranges")
    units: list[dict[str, Any]] = []
    for range_index, span in enumerate(target_ranges):
        start = int(span["start"])
        end = int(span["end"])
        if scope == "line":
            pieces = _line_ranges(text, start, end)
        elif scope == "paragraph":
            pieces = _paragraph_ranges(text, start, end)
        elif scope == "chapter":
            pieces = [{"start": start, "end": end}] if start < end else []
        else:  # validate_rules makes this unreachable, keep the failure explicit.
            raise ContractError("RULE_FIELD_INVALID", f"unsupported scope: {scope}")
        for unit_index, piece in enumerate(pieces):
            units.append(
                {
                    "unit_key": f"{target}:{scope}:{range_index}:{unit_index}:{piece['start']}:{piece['end']}",
                    "start": piece["start"],
                    "end": piece["end"],
                }
            )
    return units


def _flags_for(engine: Any, flags: Sequence[str]) -> Any:
    if callable(getattr(engine, "flags_for", None)):
        return engine.flags_for(flags)
    return 0


def _merge_intervals(intervals: Sequence[Mapping[str, int]]) -> list[dict[str, int]]:
    ordered = sorted(({"start": int(item["start"]), "end": int(item["end"])} for item in intervals), key=lambda item: (item["start"], item["end"]))
    merged: list[dict[str, int]] = []
    for item in ordered:
        if item["end"] <= item["start"]:
            raise ContractError("BOUNDARY_INVALID", "deletion interval must be non-empty")
        if merged and item["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        else:
            merged.append(item)
    return merged


def _survivor_segments(text_length: int, deleted: Sequence[Mapping[str, int]]) -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    source_cursor = 0
    cleaned_cursor = 0
    for interval in deleted:
        start = int(interval["start"])
        end = int(interval["end"])
        if source_cursor < start:
            length = start - source_cursor
            result.append(
                {
                    "source_start": source_cursor,
                    "source_end": start,
                    "cleaned_start": cleaned_cursor,
                    "cleaned_end": cleaned_cursor + length,
                }
            )
            cleaned_cursor += length
        source_cursor = end
    if source_cursor < text_length:
        length = text_length - source_cursor
        result.append(
            {
                "source_start": source_cursor,
                "source_end": text_length,
                "cleaned_start": cleaned_cursor,
                "cleaned_end": cleaned_cursor + length,
            }
        )
    return result


def _execute_cleaning(
    data: Mapping[str, Any],
    normalized_rules: Mapping[str, Any],
    limits: Mapping[str, int],
    *,
    engine: Any | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    _require_sdk()
    locked_engine = _locked_regex_engine(engine)
    text = data["source_text"]
    body_ranges = data["body_ranges"] or [{"start": 0, "end": len(text)}]
    title_ranges = data["title_ranges"]
    excluded = set(data["profile"]["excluded_hit_ids"])
    all_rows: list[dict[str, Any]] = []
    deletion_intervals: list[dict[str, int]] = []
    import_started = time.monotonic()
    scanned_total = 0
    for rule in normalized_rules["rules"]:
        if cancelled is not None and cancelled():
            raise _Cancelled()
        if not rule["enabled"] or ("all" not in rule["input_kinds"] and data["input_kind"] not in rule["input_kinds"]):
            continue
        pattern_size = len(rule["pattern"].encode("utf-8"))
        if pattern_size > limits["max_pattern_bytes"]:
            raise ContractError("LIMIT_EXCEEDED", f"pattern exceeds max_pattern_bytes: {rule['id']}")
        rule_started = time.monotonic()
        try:
            compiled = locked_engine.compile(rule["pattern"], flags=_flags_for(locked_engine, rule["flags"]))
        except RegexRuntimeUnavailable:
            raise
        except Exception as exc:
            raise ContractError("REGEX_COMPILE_INVALID", f"cannot compile rule {rule['id']}") from exc
        rule_scanned = 0
        rule_matches = 0
        units = _scope_units(
            text,
            scope=rule["scope"],
            target=rule["target"],
            body_ranges=body_ranges,
            title_ranges=title_ranges,
        )
        for unit in units:
            if cancelled is not None and cancelled():
                raise _Cancelled()
            if time.monotonic() - import_started > limits["import_timeout_ms"] / 1000:
                raise ContractError("TIMEOUT", "import timeout exceeded before native regex call")
            unit_text = text[unit["start"] : unit["end"]]
            unit_size = len(unit_text)
            if unit_size > limits["max_scope_codepoints"]:
                raise ContractError("LIMIT_EXCEEDED", f"scope exceeds max_scope_codepoints: {unit['unit_key']}")
            rule_scanned += unit_size
            scanned_total += unit_size
            if rule_scanned > limits["max_rule_scanned_codepoints"]:
                raise ContractError("LIMIT_EXCEEDED", f"rule exceeds max_rule_scanned_codepoints: {rule['id']}")
            if scanned_total > limits["max_import_scanned_codepoints"]:
                raise ContractError("LIMIT_EXCEEDED", "import exceeds max_import_scanned_codepoints")
            if not unit_text:
                continue
            try:
                iterator = compiled.finditer(
                    unit_text,
                    overlapped=False,
                    timeout=limits["scope_timeout_ms"] / 1000,
                )
                for occurrence, match in enumerate(iterator):
                    if cancelled is not None and cancelled():
                        raise _Cancelled()
                    local_start = match.start()
                    local_end = match.end()
                    if isinstance(local_start, bool) or isinstance(local_end, bool) or not isinstance(local_start, int) or not isinstance(local_end, int):
                        raise ContractError("REGEX_RUNTIME_INVALID", "regex match boundaries are not integers")
                    if local_start < 0 or local_end > len(unit_text) or local_end <= local_start:
                        raise ContractError("EMPTY_MATCH", f"rule {rule['id']} produced an empty/out-of-range match")
                    start = unit["start"] + local_start
                    end = unit["start"] + local_end
                    quote = text[start:end]
                    hit_id = _hash_jcs(
                        "source-cleaning-hit-id/v1",
                        {
                            "schema": "source-cleaning-hit-id/v1",
                            "rules_hash": data["profile"]["rules_hash"],
                            "rule_id": rule["id"],
                            "rule_order": rule["order"],
                            "unit_key": unit["unit_key"],
                            "occurrence": occurrence,
                            "start_codepoint": start,
                            "end_codepoint": end,
                             "quote_hash": _sha256_text(quote),
                        },
                    )
                    row = {
                        "hit_id": hit_id,
                        "rule_id": rule["id"],
                        "rule_order": rule["order"],
                        "scope": rule["scope"],
                        "target": rule["target"],
                        "start_codepoint": start,
                        "end_codepoint": end,
                        "quote": quote,
                         "quote_hash": _sha256_text(quote),
                        "excluded": hit_id in excluded,
                    }
                    validate_match_row(row, text_length=len(text), path=f"match_rows[{len(all_rows)}]")
                    all_rows.append(row)
                    rule_matches += 1
                    if rule_matches > limits["max_rule_matches"]:
                        raise ContractError("LIMIT_EXCEEDED", f"rule matches exceed max_rule_matches: {rule['id']}")
                    if len(all_rows) > limits["max_import_matches"] or len(all_rows) > limits["max_persisted_matches"]:
                        raise ContractError("LIMIT_EXCEEDED", "matches exceed the import/persisted match budget")
                    if not row["excluded"]:
                        deletion_intervals.append({"start": start, "end": end})
                    if occurrence + 1 > limits["max_scope_matches"]:
                        raise ContractError("LIMIT_EXCEEDED", f"scope matches exceed max_scope_matches: {unit['unit_key']}")
            except TimeoutError as exc:
                raise ContractError("TIMEOUT", f"native regex timeout in rule {rule['id']}") from exc
        if time.monotonic() - rule_started > limits["rule_timeout_ms"] / 1000:
            raise ContractError("TIMEOUT", f"rule timeout exceeded: {rule['id']}")

    deleted = _merge_intervals(deletion_intervals)
    deleted_codepoints = sum(item["end"] - item["start"] for item in deleted)
    if deleted_codepoints > data["profile"]["thresholds"]["max_deleted_codepoints"]:
        raise ContractError("THRESHOLD_BLOCK", "deleted codepoints exceed profile threshold")
    cleaned_text = "".join(
        text[cursor : interval["start"]]
        for cursor, interval in _replacement_parts(len(text), deleted)
    )
    # _replacement_parts yields a final slice boundary; the expression above
    # intentionally keeps the reconstruction independent from line endings.
    if deleted:
        chunks: list[str] = []
        cursor = 0
        for interval in deleted:
            chunks.append(text[cursor : interval["start"]])
            cursor = interval["end"]
        chunks.append(text[cursor:])
        cleaned_text = "".join(chunks)
    if len(all_rows) > data["profile"]["thresholds"]["max_total_matches"]:
        raise ContractError("THRESHOLD_BLOCK", "matches exceed profile threshold")
    manifest_body = {
        "schema": SOURCE_CLEANING_MATCH_MANIFEST_SCHEMA,
        "source_asset_hash": data["source_asset_hash"],
        "source_text_hash": _sha256_text(text),
        "rules_hash": data["profile"]["rules_hash"],
        "profile_hash": data["profile"]["profile_hash"],
        "rows": all_rows,
        "deletion_spans": deleted,
    }
    match_manifest = dict(manifest_body)
    match_manifest["manifest_hash"] = _hash_jcs(SOURCE_CLEANING_MATCH_MANIFEST_SCHEMA, manifest_body)
    cleaned_hash = _sha256_text(cleaned_text)
    receipt_body = {
        "schema": SOURCE_CLEANING_RECEIPT_SCHEMA,
        "source_asset_id": data["source_asset_id"],
        "source_asset_hash": data["source_asset_hash"],
        "source_text_hash": _sha256_text(text),
        "rules_release_id": data["rules_release_id"],
        "rules_package_hash": data["rules_package_hash"],
        "rules_hash": data["profile"]["rules_hash"],
        "profile_hash": data["profile"]["profile_hash"],
        "rule_order": list(data["profile"]["rule_order"]),
        "match_manifest_hash": match_manifest["manifest_hash"],
        "survivor_segments": _survivor_segments(len(text), deleted),
        "provisional_text_hash": _sha256_text(text),
        "canonical_text_hash": cleaned_hash,
    }
    receipt = dict(receipt_body)
    receipt["receipt_hash"] = _hash_jcs(SOURCE_CLEANING_RECEIPT_SCHEMA, receipt_body)
    runtime_receipt = {
        "engine": CORE_REGEX_ENGINE,
        "package": CORE_REGEX_PACKAGE,
        "dependency_version": CORE_REGEX_VERSION,
        "syntax_version": CORE_REGEX_SYNTAX_VERSION,
        "native_timeout": True,
        "unicode_version": unicodedata.unidata_version,
        "flags_contract": list(REGEX_FLAG_ORDER),
    }
    return {
        "schema": SOURCE_CLEANING_PREVIEW_ARTIFACT_SCHEMA,
        "run_snapshot_hash": data["run_snapshot_hash"],
        "source_asset_id": data["source_asset_id"],
        "source_asset_hash": data["source_asset_hash"],
        "source_text_hash": _sha256_text(text),
        "cleaned_text": cleaned_text,
        "cleaned_text_hash": cleaned_hash,
        "canonical_text_hash": cleaned_hash,
        "rules_release_id": data["rules_release_id"],
        "rules_package_hash": data["rules_package_hash"],
        "rules_hash": data["profile"]["rules_hash"],
        "profile_hash": data["profile"]["profile_hash"],
        "rule_order": list(data["profile"]["rule_order"]),
        "match_rows": all_rows,
        "deletion_spans": deleted,
        "survivor_segments": _survivor_segments(len(text), deleted),
        "match_manifest_hash": match_manifest["manifest_hash"],
        "match_manifest": match_manifest,
        "receipt": receipt,
        "runtime": runtime_receipt,
    }


def _replacement_parts(text_length: int, deleted: Sequence[Mapping[str, int]]) -> list[tuple[int, dict[str, int]]]:
    """Return deterministic boundaries for the no-deletion fast path."""

    if not deleted:
        return [(0, {"start": text_length, "end": text_length})]
    return [(0, {"start": 0, "end": 0})]


def _host_call(host: HostPort, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    if host is None or not callable(getattr(host, "call", None)):
        raise HostBindingError(f"{method} requires HostPort.call(method, params)")
    try:
        result = host.call(method, dict(params))
    except ContractError:
        raise
    except Exception as exc:
        raise HostBindingError(f"{method} failed before returning a result") from exc
    if not isinstance(result, Mapping):
        raise HostBindingError(f"{method} returned a non-object")
    assert _sdk_validate_rpc_result is not None
    try:
        _sdk_validate_rpc_result(method, result)
    except Exception as exc:
        raise HostBindingError(f"{method} result failed public validate_rpc_result: {exc}") from exc
    return dict(result)


def _read_asset(host: HostPort, asset_id: str, expected_hash: str, *, max_bytes: int | None = None) -> bytes:
    """Read a Core Asset as contiguous, per-page authenticated UTF-8 input."""

    _id(asset_id, "asset_id")
    _hash(expected_hash, "expected_hash")
    offset = 0
    chunks: list[bytes] = []
    for _ in range(_MAX_ASSET_READ_PAGES):
        if max_bytes is not None:
            _integer(max_bytes, "max_bytes", minimum=1)
            remaining = max_bytes - offset
            if remaining < 0:
                raise ContractError("LIMIT_EXCEEDED", "source Asset exceeds max_snapshot_bytes")
            requested_length = min(_ASSET_READ_LENGTH, remaining + 1)
        else:
            requested_length = _ASSET_READ_LENGTH
        response = _host_call(
            host,
            "host.asset.read/v1",
            {"asset_id": asset_id, "offset": offset, "length": requested_length},
        )
        encoded = response.get("base64_chunk")
        if not isinstance(encoded, str):
            raise HostBindingError("host.asset.read/v1 returned non-base64 source data")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise HostBindingError("host.asset.read/v1 returned invalid base64 source data") from exc
        if len(chunk) > requested_length:
            raise HostBindingError("host.asset.read/v1 returned a page larger than requested")
        if response.get("content_hash") != _sha256(chunk):
            raise HostBindingError("host.asset.read/v1 page content_hash mismatch")
        next_offset = response.get("next_offset")
        expected_next = offset + len(chunk)
        if max_bytes is not None and expected_next > max_bytes:
            raise ContractError("LIMIT_EXCEEDED", "source Asset exceeds max_snapshot_bytes")
        if next_offset is None:
            if not chunk:
                raise HostBindingError("host.asset.read/v1 made no progress at EOF")
            chunks.append(chunk)
            offset = expected_next
            break
        if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset != expected_next or next_offset <= offset:
            raise HostBindingError("host.asset.read/v1 pages are not contiguous")
        chunks.append(chunk)
        offset = next_offset
    else:
        raise HostBindingError("host.asset.read/v1 did not terminate at EOF")
    raw = b"".join(chunks)
    if len(raw) != offset or _sha256(raw) != expected_hash:
        raise ContractError("HASH_BINDING_INVALID", "complete source Asset hash does not match source_asset_hash")
    return raw


def _upload(host: HostPort, context: _RunContext, raw: bytes, *, mime: str, suffix: str) -> str:
    expected_hash = _sha256(raw)
    upload_id = f"{context.request_hash}-upload-{suffix}"
    created = _host_call(
        host,
        "host.asset.create/v1",
        {
            "operation_key": context.request_hash,
            "upload_id": upload_id,
            "offset": 0,
            "mime": mime,
            "total_size": len(raw),
            "expected_hash": expected_hash,
            "chunk_hash": expected_hash,
            "base64_chunk": base64.b64encode(raw).decode("ascii"),
            "final": True,
        },
    )
    asset_id = created.get("asset_id")
    if (
        created.get("upload_id") != upload_id
        or created.get("accepted_bytes") != len(raw)
        or created.get("completed") is not True
        or not isinstance(asset_id, str)
        or not asset_id
    ):
        raise HostBindingError("host.asset.create/v1 returned an upload identity/size mismatch")
    _id(asset_id, "created_asset.asset_id")
    status = _host_call(
        host,
        "host.asset.upload.status/v1",
        {"upload_id": upload_id, "expected_hash": expected_hash},
    )
    if status.get("accepted_bytes") != len(raw) or status.get("completed") is not True or status.get("asset_id") != asset_id:
        raise HostBindingError("host.asset.upload.status/v1 did not confirm the complete upload")
    return asset_id


def _host_asset(host: HostPort, context: _RunContext, payload: Mapping[str, Any], *, mime: str, suffix: str) -> tuple[str, str]:
    raw = _canonical(payload)
    return _upload(host, context, raw, mime=mime, suffix=suffix), _sha256(raw)


def read_asset_json(host: Any, asset_id: str, expected_hash: str) -> dict[str, Any]:
    """Read an output payload through the same frozen HostPort Asset path."""

    _require_sdk()
    raw = _read_asset(host, asset_id, expected_hash)
    assert _sdk_parse_json_bytes is not None
    try:
        parsed = _sdk_parse_json_bytes(raw)
    except Exception as exc:
        raise HostBindingError("result Asset is not UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise HostBindingError("result Asset JSON must be an object")
    return parsed


def _producer(data: Mapping[str, Any], capability: str) -> dict[str, Any]:
    identity = load_identity()
    return {
        "plugin_id": PLUGIN_ID,
        "release_id": identity["release_id"],
        "capability_id": capability,
        "job_id": data["job_id"],
        "step_id": data["step_id"],
        "attempt_id": data["attempt_id"],
        "lease_epoch": data["lease_epoch"],
    }


def _source_refs(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "workspace_id": data["workspace_id"],
            "source_type": "source_asset",
            "source_id": data["source_asset_id"],
            "revision_or_hash": data["source_asset_hash"],
        },
        {
            "workspace_id": data["workspace_id"],
            "source_type": "document_revision",
            "source_id": data["document_id"],
            "revision_or_hash": data["revision_id"],
        },
    ]


def _bundle(
    data: Mapping[str, Any],
    *,
    capability: str,
    contract_id: str,
    items: list[dict[str, Any]],
    partial: bool = False,
) -> dict[str, Any]:
    producer = _producer(data, capability)
    seed = {
        "capability": capability,
        "snapshot": data["run_snapshot_hash"],
        "producer": producer,
        "items": items,
    }
    bundle_id = _hash_id("bundle", seed)
    bundle = {
        "schema": "result-bundle/v1",
        "contract_id": contract_id,
        "bundle_id": bundle_id,
        "bundle_type": {"artifact-bundle/v1": "artifact", "candidate-batch/v1": "candidate_batch", "diagnostic-bundle/v1": "diagnostic"}[contract_id],
        "producer": producer,
        "input_snapshot_hash": data["run_snapshot_hash"],
        "items": items,
        "warnings": [],
        "partial": partial,
        "provenance_receipt_id": data["provenance_receipt_id"],
        "skill_chain_result_refs": [],
    }
    _verify_result(
        bundle,
        workspace_id=data.get("workspace_id") if contract_id == "candidate-batch/v1" else None,
        snapshot_hash=str(data["run_snapshot_hash"]),
    )
    return bundle


def _artifact_item(data: Mapping[str, Any], context: _RunContext, *, capability: str, payload: Mapping[str, Any], artifact_kind: str, host: HostPort, source_refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    asset_id, payload_hash = _host_asset(host, context, payload, mime="application/json", suffix="payload")
    item_id = _hash_id("artifact-item", {"capability": capability, "payload_hash": payload_hash, "source": source_refs or []})
    return {
        "schema": "artifact-item/v1",
        "item_id": item_id,
        "artifact_kind": artifact_kind,
        "payload_asset_id": asset_id,
        "payload_hash": payload_hash,
        "mime": "application/json",
        "source_refs": source_refs if source_refs is not None else _source_refs(data),
        "status": "complete",
    }


def _candidate_item(data: Mapping[str, Any], context: _RunContext, *, capability: str, host: HostPort, payload: Mapping[str, Any], item_kind: str, mode: str, payload_schema: str, source_refs: list[dict[str, Any]] | None = None, target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    asset_id, payload_hash = _host_asset(host, context, payload, mime="application/json", suffix="candidate-payload")
    target_value = dict(target or {"workspace_id": data["workspace_id"], "entity_kind": item_kind, "entity_id": data["document_id"]})
    base = {"revision_id": data["revision_id"], "content_hash": data["base_content_hash"]}
    item_id = _hash_id("candidate-item", {"capability": capability, "payload_hash": payload_hash, "target": target_value, "base": base})
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
        "item_kind": item_kind,
        "target": target_value,
        "mutation": {"mode": mode, "payload_schema": payload_schema, "payload_hash": payload_hash},
        "payload_asset_id": asset_id,
        "base": base,
        "write_set": [
            {
                "workspace_id": target_value["workspace_id"],
                "entity_kind": target_value["entity_kind"],
                "entity_id": target_value["entity_id"],
                "revision_id": base["revision_id"],
                "content_hash": base["content_hash"],
            }
        ],
        "parent_candidate_ids": [],
        "source_refs": source_refs if source_refs is not None else _source_refs(data),
        "status": "complete",
    }


_APPLY_STATE_SCHEMA = "source-cleaning-apply-state/v1"
_APPLY_STATE_KEYS = {
    "schema",
    "state_kind",
    "request_hash",
    "job_id",
    "step_id",
    "attempt_id",
    "worker_run_id",
    "lease_epoch",
    "run_snapshot_hash",
    "workspace_id",
    "document_id",
    "revision_id",
    "base_content_hash",
    "source_asset_id",
    "source_asset_hash",
    "storage_source_format",
    "input_kind",
    "rules_release_id",
    "rules_package_hash",
    "rules_hash",
    "profile_hash",
    "rule_order",
    "review_context_hash",
    "review_context",
    "candidate_item",
}


def _apply_state_body(
    data: Mapping[str, Any],
    context: _RunContext,
    review: Mapping[str, Any],
    *,
    state_kind: str,
    candidate_item: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": _APPLY_STATE_SCHEMA,
        "state_kind": state_kind,
        "request_hash": context.request_hash,
        "job_id": data["job_id"],
        "step_id": data["step_id"],
        "attempt_id": data["attempt_id"],
        "worker_run_id": data["worker_run_id"],
        "lease_epoch": data["lease_epoch"],
        "run_snapshot_hash": data["run_snapshot_hash"],
        "workspace_id": data["workspace_id"],
        "document_id": data["document_id"],
        "revision_id": data["revision_id"],
        "base_content_hash": data["base_content_hash"],
        "source_asset_id": data["source_asset_id"],
        "source_asset_hash": data["source_asset_hash"],
        "storage_source_format": data["storage_source_format"],
        "input_kind": data["input_kind"],
        "rules_release_id": data["rules_release_id"],
        "rules_package_hash": data["rules_package_hash"],
        "rules_hash": data["profile"]["rules_hash"],
        "profile_hash": data["profile"]["profile_hash"],
        "rule_order": list(data["profile"]["rule_order"]),
        "review_context_hash": _hash_jcs("source-cleaning-review/v1", review),
        "review_context": deepcopy(dict(review)),
        "candidate_item": deepcopy(dict(candidate_item)) if candidate_item is not None else None,
    }
    if set(body) != _APPLY_STATE_KEYS:
        raise ContractError("CHECKPOINT_INVALID", "apply state does not have the frozen closed shape")
    return body


def _upload_apply_state(
    host: HostPort,
    context: _RunContext,
    data: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    state_kind: str,
    candidate_item: Mapping[str, Any] | None,
) -> tuple[str, str]:
    body = _apply_state_body(data, context, review, state_kind=state_kind, candidate_item=candidate_item)
    return _host_asset(host, context, body, mime="application/json", suffix=f"apply-{state_kind}-state")


def _read_json_asset(host: HostPort, asset_id: str, expected_hash: str, *, label: str) -> tuple[bytes, dict[str, Any]]:
    _require_sdk()
    raw = _read_asset(host, asset_id, expected_hash)
    assert _sdk_parse_json_bytes is not None
    try:
        parsed = _sdk_parse_json_bytes(raw)
    except Exception as exc:
        raise HostBindingError(f"{label} Asset is not UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise HostBindingError(f"{label} Asset JSON must be an object")
    return raw, parsed


def _validate_resume_candidate_item(
    data: Mapping[str, Any],
    host: HostPort,
    item: Mapping[str, Any],
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "item_id",
        "item_kind",
        "target",
        "mutation",
        "payload_asset_id",
        "base",
        "write_set",
        "parent_candidate_ids",
        "source_refs",
        "status",
    }
    if set(item) != expected_keys:
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate item has an invalid closed shape")
    if item["schema"] != "candidate-item/v1" or item["item_kind"] != "document" or item["status"] != "complete":
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate item is not a completed document mutation")
    target = item["target"]
    expected_target = {"workspace_id": data["workspace_id"], "entity_kind": "document", "entity_id": data["document_id"]}
    if target != expected_target:
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate target is not bound to the request")
    base = item["base"]
    expected_base = {"revision_id": data["revision_id"], "content_hash": data["base_content_hash"]}
    if base != expected_base:
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate base is not bound to the request")
    mutation = item["mutation"]
    if (
        not isinstance(mutation, Mapping)
        or set(mutation) != {"mode", "payload_schema", "payload_hash"}
        or mutation["mode"] != "replace"
        or mutation["payload_schema"] != SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA
    ):
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate mutation is invalid")
    payload_hash = _hash(mutation["payload_hash"], "resume_state.candidate_item.mutation.payload_hash")
    payload_asset_id = _id(item["payload_asset_id"], "resume_state.candidate_item.payload_asset_id")
    payload_raw, payload = _read_json_asset(host, payload_asset_id, payload_hash, label="resume candidate payload")
    if set(payload) != {"schema", "text", "canonical_text_hash", "cleaning_receipt"} or payload.get("schema") != SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA:
        raise ContractError("CHECKPOINT_INVALID", "resume candidate payload does not have the frozen shape")
    _string(payload["text"], "resume_state.candidate_payload.text")
    _hash(payload["canonical_text_hash"], "resume_state.candidate_payload.canonical_text_hash")
    if not isinstance(payload["cleaning_receipt"], Mapping):
        raise ContractError("CHECKPOINT_INVALID", "resume candidate payload receipt is invalid")
    _id(item["item_id"], "resume_state.candidate_item.item_id")
    expected_item_id = _hash_id(
        "candidate-item",
        {"capability": CAPABILITY_APPLY, "payload_hash": payload_hash, "target": expected_target, "base": expected_base},
    )
    if item["item_id"] != expected_item_id:
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate item identity is not deterministic")
    expected_write_set = [
        {
            "workspace_id": expected_target["workspace_id"],
            "entity_kind": expected_target["entity_kind"],
            "entity_id": expected_target["entity_id"],
            "revision_id": expected_base["revision_id"],
            "content_hash": expected_base["content_hash"],
        }
    ]
    if item["write_set"] != expected_write_set or item["parent_candidate_ids"] != [] or item["source_refs"] != _source_refs(data):
        raise ContractError("CHECKPOINT_INVALID", "resume state candidate references are not bound to the request")
    return deepcopy(dict(item))


def _resume_apply_state(
    data: Mapping[str, Any],
    context: _RunContext,
    host: HostPort,
    review: Mapping[str, Any],
) -> dict[str, Any] | None:
    checkpoint_raw, checkpoint = _read_json_asset(
        host,
        data["resume_checkpoint_asset_id"],
        data["resume_checkpoint_asset_hash"],
        label="resume checkpoint",
    )
    _verify_checkpoint(
        checkpoint,
        expected_snapshot_hash=str(data["run_snapshot_hash"]),
        previous_seq=None,
    )
    checkpoint_id = _id(checkpoint.get("checkpoint_id"), "resume_checkpoint.checkpoint_id")
    checkpoint_seq = checkpoint.get("checkpoint_seq")
    if (
        checkpoint_id not in context.checkpoint_ids
        or isinstance(checkpoint_seq, bool)
        or not isinstance(checkpoint_seq, int)
        or checkpoint_seq < 1
        or checkpoint_seq > len(context.checkpoint_ids)
        or context.checkpoint_ids[checkpoint_seq - 1] != checkpoint_id
    ):
        raise ContractError("CHECKPOINT_INVALID", "resume checkpoint identity is not issued for this request")
    for field in ("job_id", "step_id", "source_attempt_id", "lease_epoch", "run_snapshot_hash", "created_at"):
        expected = data["attempt_id"] if field == "source_attempt_id" else data[field]
        if checkpoint.get(field) != expected:
            raise ContractError("CHECKPOINT_INVALID", f"resume checkpoint {field} is not bound to the request")
    if checkpoint.get("state_asset_id") != data["resume_state_asset_id"] or checkpoint.get("unit_set_hash") != data["resume_state_asset_hash"]:
        raise ContractError("CHECKPOINT_INVALID", "resume checkpoint does not bind the requested state Asset")
    if _hash_jcs("checkpoint/v1", {key: value for key, value in checkpoint.items() if key != "checkpoint_hash"}) != checkpoint.get("checkpoint_hash"):
        raise ContractError("CHECKPOINT_INVALID", "resume checkpoint hash is invalid")
    state_raw, state = _read_json_asset(
        host,
        data["resume_state_asset_id"],
        data["resume_state_asset_hash"],
        label="resume state",
    )
    if _sha256(state_raw) != data["resume_state_asset_hash"] or set(state) != _APPLY_STATE_KEYS:
        raise ContractError("CHECKPOINT_INVALID", "resume state Asset hash or closed shape is invalid")
    expected_bindings = {
        "schema": _APPLY_STATE_SCHEMA,
        "request_hash": context.request_hash,
        "job_id": data["job_id"],
        "step_id": data["step_id"],
        "attempt_id": data["attempt_id"],
        "worker_run_id": data["worker_run_id"],
        "lease_epoch": data["lease_epoch"],
        "run_snapshot_hash": data["run_snapshot_hash"],
        "workspace_id": data["workspace_id"],
        "document_id": data["document_id"],
        "revision_id": data["revision_id"],
        "base_content_hash": data["base_content_hash"],
        "source_asset_id": data["source_asset_id"],
        "source_asset_hash": data["source_asset_hash"],
        "storage_source_format": data["storage_source_format"],
        "input_kind": data["input_kind"],
        "rules_release_id": data["rules_release_id"],
        "rules_package_hash": data["rules_package_hash"],
        "rules_hash": data["profile"]["rules_hash"],
        "profile_hash": data["profile"]["profile_hash"],
        "rule_order": list(data["profile"]["rule_order"]),
        "review_context_hash": _hash_jcs("source-cleaning-review/v1", review),
        "review_context": dict(review),
    }
    for field, expected in expected_bindings.items():
        if state.get(field) != expected:
            raise ContractError("CHECKPOINT_INVALID", f"resume state {field} is not bound to the request")
    context.last_checkpoint_seq = checkpoint_seq
    state_kind = state.get("state_kind")
    item = state.get("candidate_item")
    if state_kind == "cancelled":
        if item is not None:
            raise ContractError("CHECKPOINT_INVALID", "cancelled resume state must not contain a candidate item")
        return None
    if state_kind != "candidate" or not isinstance(item, Mapping):
        raise ContractError("CHECKPOINT_INVALID", "resume state does not contain a valid candidate item")
    return _validate_resume_candidate_item(data, host, item)


def _accepted(result: Mapping[str, object], method: str) -> None:
    if result.get("accepted") is not True:
        raise HostBindingError(f"Host rejected {method}")


def _record_event(context: _RunContext, result: Mapping[str, object], method: str) -> None:
    _accepted(result, method)
    sequence = result.get("job_event_seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < context.last_job_event_seq:
        raise HostBindingError(f"{method} job_event_seq moved backwards")
    context.last_job_event_seq = sequence


def _event(host: HostPort, context: _RunContext, event_type: str, payload_asset_id: str | None, local_seq: int) -> None:
    result = _host_call(
        host,
        "host.job.event/v1",
        {
            "operation_key": context.request_hash,
            "event_type": event_type,
            "payload_asset_id": payload_asset_id,
            "local_seq": local_seq,
        },
    )
    _record_event(context, result, "host.job.event/v1")


def _checkpoint(
    host: HostPort,
    request: Mapping[str, Any],
    context: _RunContext,
    *,
    state_asset_id: str | None,
    state_asset_hash: str | None = None,
    sequence: int = 1,
) -> dict[str, Any]:
    if sequence > len(context.checkpoint_ids):
        raise ContractError("CHECKPOINT_INVALID", "Core did not issue a checkpoint identity for this run")
    checkpoint_id = context.checkpoint_ids[sequence - 1]
    checkpoint: dict[str, Any] = {
        "schema": "checkpoint/v1",
        "checkpoint_id": checkpoint_id,
        "checkpoint_seq": sequence,
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "source_attempt_id": request["attempt_id"],
        "lease_epoch": request["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "replay_policy": "checkpoint_resume",
        "completed_units": sequence,
        "total_units": request["total_units"],
        "unit_set_hash": state_asset_hash,
        "state_asset_id": state_asset_id,
        "created_at": request["created_at"],
    }
    checkpoint["checkpoint_hash"] = _hash_jcs("checkpoint/v1", checkpoint)
    _verify_checkpoint(
        checkpoint,
        expected_snapshot_hash=str(request["run_snapshot_hash"]),
        previous_seq=context.last_checkpoint_seq or None,
    )
    checkpoint_asset_id = _upload(
        host,
        context,
        _canonical(checkpoint),
        mime="application/json",
        suffix=f"checkpoint-{sequence}",
    )
    response = _host_call(
        host,
        "host.checkpoint.commit/v1",
        {"operation_key": context.request_hash, "checkpoint_asset_id": checkpoint_asset_id},
    )
    _accepted(response, "host.checkpoint.commit/v1")
    if (
        response.get("checkpoint_id") != checkpoint_id
        or response.get("completed_units") != checkpoint["completed_units"]
        or response.get("total_units") != checkpoint["total_units"]
    ):
        raise HostBindingError("host.checkpoint.commit/v1 returned a different checkpoint identity")
    _record_event(context, response, "host.checkpoint.commit/v1")
    context.last_checkpoint_seq = sequence
    return checkpoint


def _provenance_receipt(
    data: Mapping[str, Any],
    *,
    capability: str,
    bundle: Mapping[str, Any] | None,
    staged_items: Sequence[str] = (),
) -> dict[str, Any]:
    staged_item_ids = [
        _id(item_id, f"provenance.staged_items[{index}]")
        for index, item_id in enumerate(staged_items)
    ]
    if len(staged_item_ids) != len(set(staged_item_ids)):
        raise ContractError("STAGED_ITEM_DUPLICATE", "provenance staged item IDs must be unique")
    verified_identity = load_identity()
    receipt: dict[str, Any] = {
        "schema": "provenance-receipt/v1",
        "receipt_id": data["provenance_receipt_id"],
        "plugin_id": PLUGIN_ID,
        "release_id": verified_identity["release_id"],
        "package_hash": verified_identity["package_hash"],
        "capability_id": capability,
        "job_id": data["job_id"],
        "step_id": data["step_id"],
        "attempt_id": data["attempt_id"],
        "lease_epoch": data["lease_epoch"],
        "run_snapshot_hash": data["run_snapshot_hash"],
        "bundle_id": bundle["bundle_id"] if bundle is not None else None,
        "bundle_hash": _hash_jcs("result-bundle/v1", bundle) if bundle is not None else None,
        "parent_receipt_ids": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": staged_item_ids,
        "created_at": data["created_at"],
    }
    receipt["receipt_hash"] = _hash_jcs("provenance-receipt/v1", receipt)
    _verify_receipt(receipt)
    return receipt


def _complete(
    host: HostPort,
    request: Mapping[str, Any],
    context: _RunContext,
    *,
    outcome: str,
    result_asset_id: str | None,
    terminal_detail_asset_id: str | None,
    candidate_stage_operation_key: str | None,
    local_seq: int,
) -> None:
    if context.terminal_outcome is not None:
        raise ContractError("TERMINAL_ALREADY_COMMITTED", "worker run already committed a terminal outcome")
    response = _host_call(
        host,
        "host.job.complete/v1",
        {
            "operation_key": context.request_hash,
            "worker_run_id": request["worker_run_id"],
            "outcome": outcome,
            "result_bundle_asset_id": result_asset_id,
            "candidate_stage_operation_key": candidate_stage_operation_key,
            "terminal_detail_asset_id": terminal_detail_asset_id,
            "local_seq": local_seq,
        },
    )
    # A returned response proves the Host observed this terminal RPC.  Never
    # issue a second completion merely because its acknowledgement is invalid.
    context.terminal_outcome = outcome
    _accepted(response, "host.job.complete/v1")
    if (
        response.get("provenance_receipt_id") != request["provenance_receipt_id"]
        or response.get("attempt_state") != outcome
        or response.get("step_state") != outcome
        or response.get("job_state") != outcome
    ):
        raise HostBindingError("host.job.complete/v1 did not preserve terminal identity")
    if outcome == "succeeded" and not isinstance(result_asset_id, str):
        raise HostBindingError("succeeded job completion requires a result Bundle Asset")
    event_sequence = response.get("job_event_seq")
    high_water = response.get("core_event_high_water")
    if (
        isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or event_sequence < context.last_job_event_seq
        or isinstance(high_water, bool)
        or not isinstance(high_water, int)
        or high_water < event_sequence
    ):
        raise HostBindingError("host.job.complete/v1 returned invalid event high-water")
    context.last_job_event_seq = event_sequence


def _stage_candidate(
    host: HostPort,
    data: Mapping[str, Any],
    context: _RunContext,
    bundle_asset_id: str,
    *,
    expected_bundle_id: str,
    expected_item_ids: Sequence[str],
) -> str:
    _id(expected_bundle_id, "stage_request.expected_bundle_id")
    expected_ids = [_id(item_id, f"expected_candidate_item_ids[{index}]") for index, item_id in enumerate(expected_item_ids)]
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        raise ContractError("CANDIDATE_STAGE_INVALID", "Candidate Bundle must contain unique item IDs before staging")
    stage_key = f"{context.request_hash}-candidate-stage"
    response = _host_call(
        host,
        "host.candidate.stage/v1",
        {
            "operation_key": stage_key,
            "result_bundle_asset_id": bundle_asset_id,
            "input_snapshot_hash": data["run_snapshot_hash"],
        },
    )
    _accepted(response, "host.candidate.stage/v1")
    staged_items = response.get("staged_items")
    if not isinstance(staged_items, list):
        raise HostBindingError("host.candidate.stage/v1 staged_items is not an array")
    if not staged_items:
        raise HostBindingError("host.candidate.stage/v1 must return one receipt row per Candidate Bundle item")
    actual_ids: list[str] = []
    for index, item in enumerate(staged_items):
        if not isinstance(item, Mapping) or set(item) != {"item_id", "candidate_id", "stage_status", "publication_eligibility"}:
            raise HostBindingError(f"host.candidate.stage/v1 staged_items[{index}] is not the frozen stage receipt object")
        actual_ids.append(_id(item["item_id"], f"stage_response.staged_items[{index}].item_id"))
        _id(item["candidate_id"], f"stage_response.staged_items[{index}].candidate_id")
        if item["stage_status"] not in {"created", "existing"}:
            raise HostBindingError("host.candidate.stage/v1 must return successful stage_status rows only")
        if item["publication_eligibility"] not in {"eligible", "review_only", "none"}:
            raise HostBindingError("host.candidate.stage/v1 returned an invalid publication_eligibility")
    if actual_ids != expected_ids:
        raise HostBindingError(
            "host.candidate.stage/v1 staged_items must exactly match the emitted Candidate Bundle item IDs"
        )
    context.stage_response = dict(response)  # keep the RPC receipt separate from provenance staged_items
    _record_event(context, response, "host.candidate.stage/v1")
    return stage_key


def _staged_item_ids(context: _RunContext) -> list[str]:
    """Project validated stage receipt objects to provenance item references."""
    response = context.stage_response
    if response is None:
        return []
    staged_items = response.get("staged_items")
    if not isinstance(staged_items, list):
        raise HostBindingError("host.candidate.stage/v1 staged_items is not an array")
    item_ids: list[str] = []
    for index, item in enumerate(staged_items):
        if not isinstance(item, Mapping):
            raise HostBindingError(f"host.candidate.stage/v1 staged_items[{index}] is not an object")
        item_ids.append(_id(item.get("item_id"), f"stage_response.staged_items[{index}].item_id"))
    if len(item_ids) != len(set(item_ids)):
        raise ContractError("STAGED_ITEM_DUPLICATE", "stage response item IDs must be unique")
    return item_ids


def _finalize(
    data: Mapping[str, Any],
    context: _RunContext,
    *,
    capability: str,
    contract_id: str,
    items: list[dict[str, Any]],
    host: HostPort,
    stage_candidate: bool,
    local_seq: int,
) -> dict[str, Any]:
    bundle = _bundle(data, capability=capability, contract_id=contract_id, items=items)
    bundle_asset_id = _upload(
        host,
        context,
        _canonical(bundle),
        mime="application/json",
        suffix="result-bundle",
    )
    stage_key: str | None = None
    if stage_candidate:
        stage_key = _stage_candidate(
            host,
            data,
            context,
            bundle_asset_id,
            expected_bundle_id=str(bundle["bundle_id"]),
            expected_item_ids=[str(item["item_id"]) for item in items],
        )
    _provenance_receipt(
        data,
        capability=capability,
        bundle=bundle,
        staged_items=_staged_item_ids(context) if stage_candidate else (),
    )
    _complete(
        host,
        data,
        context,
        outcome="succeeded",
        result_asset_id=bundle_asset_id,
        terminal_detail_asset_id=None,
        candidate_stage_operation_key=stage_key,
        local_seq=local_seq,
    )
    return bundle


def _cancel_terminal(host: HostPort, request: Mapping[str, Any], context: _RunContext, *, capability: str) -> None:
    if capability == CAPABILITY_APPLY:
        try:
            if context.last_checkpoint_seq == 0:
                review = _validate_review(request["review_context"])
                state_asset_id, state_asset_hash = _upload_apply_state(
                    host,
                    context,
                    request,
                    review,
                    state_kind="cancelled",
                    candidate_item=None,
                )
                _checkpoint(
                    host,
                    request,
                    context,
                    state_asset_id=state_asset_id,
                    state_asset_hash=state_asset_hash,
                )
        except Exception as checkpoint_error:
            # A cancelled apply is resumable only after Core accepts an
            # authoritative checkpoint that names the state Asset.  If that
            # commit fails, publish an explicit terminal failure instead of
            # claiming cancellation or silently swallowing the host error.
            try:
                _provenance_receipt(request, capability=capability, bundle=None)
                _complete(
                    host,
                    request,
                    context,
                    outcome="failed",
                    result_asset_id=None,
                    terminal_detail_asset_id=None,
                    candidate_stage_operation_key=None,
                    local_seq=2,
                )
            except Exception as terminal_error:
                raise HostBindingError(
                    "apply cancellation checkpoint failed and terminal failure could not be committed"
                ) from terminal_error
            raise HostBindingError(
                "apply cancellation could not commit an authoritative checkpoint/state"
            ) from checkpoint_error
        _provenance_receipt(request, capability=capability, bundle=None)
        _complete(
            host,
            request,
            context,
            outcome="cancelled",
            result_asset_id=None,
            terminal_detail_asset_id=None,
            candidate_stage_operation_key=None,
            local_seq=2,
        )
        return

    try:
        if context.last_checkpoint_seq == 0:
            _checkpoint(host, request, context, state_asset_id=None)
    except Exception:
        # Preview/merge cancellation has no resumable Candidate state.  Keep
        # the historical terminal path, while apply above remains fail-closed.
        pass
    _provenance_receipt(request, capability=capability, bundle=None)
    _complete(
        host,
        request,
        context,
        outcome="cancelled",
        result_asset_id=None,
        terminal_detail_asset_id=None,
        candidate_stage_operation_key=None,
        local_seq=2,
    )


def _failure_terminal(host: HostPort, request: Mapping[str, Any], context: _RunContext, *, capability: str, error: BaseException) -> dict[str, Any] | None:
    """Attempt a diagnostic-only terminal result; never stage a Candidate."""

    if context.terminal_outcome is not None:
        return None
    try:
        error_body = {"code": type(error).__name__, "message": str(error)[:1024]}
        detail_bytes = _canonical(error_body)
        detail_asset_id = _upload(host, context, detail_bytes, mime="application/json", suffix="failure-detail")
        diagnostic_item = {
            "schema": "diagnostic-item/v1",
            "item_id": _hash_id("diagnostic-item", {"capability": capability, "request": context.request_hash}),
            "severity": "error",
            "code": type(error).__name__,
            "message": str(error)[:1024],
            "details_asset_id": detail_asset_id,
            "details_hash": _sha256(detail_bytes),
            "source_refs": [],
            "status": "failed",
        }
        bundle = _bundle(
            request,
            capability=capability,
            contract_id="diagnostic-bundle/v1",
            items=[diagnostic_item],
            partial=True,
        )
        bundle_asset_id = _upload(host, context, _canonical(bundle), mime="application/json", suffix="failure-bundle")
        _provenance_receipt(request, capability=capability, bundle=bundle)
        _complete(
            host,
            request,
            context,
            outcome="failed",
            result_asset_id=bundle_asset_id,
            terminal_detail_asset_id=detail_asset_id,
            candidate_stage_operation_key=None,
            local_seq=2,
        )
        return bundle
    except Exception:
        try:
            _provenance_receipt(request, capability=capability, bundle=None)
            _complete(
                host,
                request,
                context,
                outcome="failed",
                result_asset_id=None,
                terminal_detail_asset_id=None,
                candidate_stage_operation_key=None,
                local_seq=2,
            )
        except Exception:
            pass
        return None


def _run_preview(request: Mapping[str, Any], host: HostPort, *, engine: Any | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any] | None:
    context = _context_for_request(request, capability=CAPABILITY_PREVIEW)
    try:
        data, normalized_rules, limits = _validate_context(request, host, capability=CAPABILITY_PREVIEW)
        if cancelled is not None and cancelled():
            _cancel_terminal(host, data, context, capability=CAPABILITY_PREVIEW)
            return None
        _event(host, context, "source-cleaning.started", None, 1)
        payload = _execute_cleaning(data, normalized_rules, limits, engine=engine, cancelled=cancelled)
        item = _artifact_item(
            data,
            context,
            capability=CAPABILITY_PREVIEW,
            payload=payload,
            artifact_kind="source-clean-preview/v1",
            host=host,
        )
        _event(host, context, "source-cleaning.payload", item["payload_asset_id"], 2)
        _checkpoint(host, data, context, state_asset_id=item["payload_asset_id"])
        return _finalize(
            data,
            context,
            capability=CAPABILITY_PREVIEW,
            contract_id="artifact-bundle/v1",
            items=[item],
            host=host,
            stage_candidate=False,
            local_seq=3,
        )
    except _Cancelled:
        _cancel_terminal(host, request, context, capability=CAPABILITY_PREVIEW)
        return None
    except Exception as exc:
        _failure_terminal(host, request, context, capability=CAPABILITY_PREVIEW, error=exc)
        raise


def preview(request: Mapping[str, Any], host: HostPort, *, engine: Any | None = None) -> dict[str, Any]:
    """Execute ``source.clean.preview/v1`` and return artifact-bundle/v1."""

    result = _run_preview(request, host, engine=engine)
    if result is None:
        raise ContractError("CANCELLED", "source.clean.preview/v1 was cancelled")
    return result


def build_review_context(preview_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project the immutable preview artifact into the apply parity fence."""

    required = {
        "schema",
        "run_snapshot_hash",
        "source_asset_hash",
        "source_text_hash",
        "rules_release_id",
        "rules_package_hash",
        "rules_hash",
        "profile_hash",
        "rule_order",
        "match_rows",
        "match_manifest_hash",
        "cleaned_text_hash",
        "canonical_text_hash",
    }
    if not isinstance(preview_payload, Mapping) or preview_payload.get("schema") != SOURCE_CLEANING_PREVIEW_ARTIFACT_SCHEMA:
        raise ContractError("PREVIEW_PAYLOAD_INVALID", "review context must be projected from a preview artifact")
    if not required.issubset(set(preview_payload)):
        raise ContractError("PREVIEW_PAYLOAD_INVALID", "preview artifact lacks parity fields")
    return {
        "schema": "source-cleaning-review/v1",
        **{key: deepcopy(preview_payload[key]) for key in sorted(required - {"schema"})},
    }


def _validate_review(review: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed(review, _REVIEW_KEYS, "review_context")
    if data["schema"] != "source-cleaning-review/v1":
        raise ContractError("REVIEW_CONTEXT_INVALID", "review_context.schema is unsupported")
    for field in ("source_asset_hash", "source_text_hash", "rules_release_id", "rules_package_hash", "rules_hash", "profile_hash", "match_manifest_hash", "cleaned_text_hash", "canonical_text_hash"):
        _hash(data[field], f"review_context.{field}")
    _hash(data["run_snapshot_hash"], "review_context.run_snapshot_hash")
    if not isinstance(data["rule_order"], list) or any(not isinstance(item, str) for item in data["rule_order"]):
        raise ContractError("REVIEW_CONTEXT_INVALID", "review_context.rule_order is invalid")
    if not isinstance(data["match_rows"], list):
        raise ContractError("REVIEW_CONTEXT_INVALID", "review_context.match_rows must be an array")
    return data


def _run_apply(
    request: Mapping[str, Any],
    host: HostPort,
    *,
    engine: Any | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    context = _context_for_request(request, capability=CAPABILITY_APPLY)
    try:
        data, normalized_rules, limits = _validate_context(request, host, capability=CAPABILITY_APPLY)
        review = _validate_review(data["review_context"])
        if cancelled is not None and cancelled():
            _cancel_terminal(host, data, context, capability=CAPABILITY_APPLY)
            return None
        if data["operation"] == "resume":
            item = _resume_apply_state(data, context, host, review)
            _event(
                host,
                context,
                "source-cleaning.resumed",
                item["payload_asset_id"] if item is not None else None,
                context.last_checkpoint_seq + 1,
            )
            if item is not None:
                return _finalize(
                    data,
                    context,
                    capability=CAPABILITY_APPLY,
                    contract_id="candidate-batch/v1",
                    items=[item],
                    host=host,
                    stage_candidate=True,
                    local_seq=context.last_checkpoint_seq + 2,
                )
        else:
            _event(host, context, "source-cleaning.started", None, 1)
        payload = _execute_cleaning(data, normalized_rules, limits, engine=engine, cancelled=cancelled)
        current_review = build_review_context(payload)
        if current_review != review:
            raise ContractError(
                "PARITY_MISMATCH",
                "apply requires the same rules, profile, source input, match rows, and canonical hash as preview",
            )
        candidate_payload = {
            "schema": SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA,
            "text": payload["cleaned_text"],
            "canonical_text_hash": payload["canonical_text_hash"],
            "cleaning_receipt": payload["receipt"],
        }
        item = _candidate_item(
            data,
            context,
            capability=CAPABILITY_APPLY,
            host=host,
            payload=candidate_payload,
            item_kind="document",
            mode="replace",
            payload_schema=SOURCE_CLEANING_APPLY_PAYLOAD_SCHEMA,
        )
        state_asset_id, state_asset_hash = _upload_apply_state(
            host,
            context,
            data,
            review,
            state_kind="candidate",
            candidate_item=item,
        )
        payload_local_seq = context.last_checkpoint_seq + 2
        _event(host, context, "source-cleaning.payload", item["payload_asset_id"], payload_local_seq)
        _checkpoint(
            host,
            data,
            context,
            state_asset_id=state_asset_id,
            state_asset_hash=state_asset_hash,
            sequence=context.last_checkpoint_seq + 1,
        )
        return _finalize(
            data,
            context,
            capability=CAPABILITY_APPLY,
            contract_id="candidate-batch/v1",
            items=[item],
            host=host,
            stage_candidate=True,
            local_seq=payload_local_seq + 1,
        )
    except _Cancelled:
        _cancel_terminal(host, request, context, capability=CAPABILITY_APPLY)
        return None
    except Exception as exc:
        _failure_terminal(host, request, context, capability=CAPABILITY_APPLY, error=exc)
        raise


def apply(request: Mapping[str, Any], host: HostPort, *, engine: Any | None = None) -> dict[str, Any]:
    """Execute ``source.clean.apply/v1`` and return the only cleaning Candidate path."""

    result = _run_apply(request, host, engine=engine)
    if result is None:
        raise ContractError("CANCELLED", "source.clean.apply/v1 was cancelled")
    return result


def _validate_merge_request(request: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = _closed(request, _MERGE_KEYS, "merge_request")
    if data["schema"] != "source.clean.rules.merge-request/v1" or data["operation_key"] != CAPABILITY_MERGE or data["operation"] not in {"run", "validate"}:
        raise ContractError("REQUEST_SCHEMA_INVALID", "invalid source.clean.rules.merge request identity")
    _context_for_request(data, capability=CAPABILITY_MERGE)
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "workspace_id"):
        _id(data[field], f"merge_request.{field}")
    _integer(data["lease_epoch"], "merge_request.lease_epoch", minimum=1)
    _hash(data["run_snapshot_hash"], "merge_request.run_snapshot_hash")
    _id(data["provenance_receipt_id"], "merge_request.provenance_receipt_id")
    _string(data["created_at"], "merge_request.created_at", maximum=64)
    _integer(data["total_units"], "merge_request.total_units", minimum=0)
    packages = data["rules_packages"]
    if not isinstance(packages, list) or not packages or len(packages) > DEFAULT_LIMITS["max_plugins"]:
        raise ContractError("RULE_PACKAGE_INVALID", "rules_packages must be a bounded non-empty array")
    seen_ids: set[str] = set()
    seen_releases: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(packages):
        package = _closed(raw, _MERGE_PACKAGE_KEYS, f"merge_request.rules_packages[{index}]")
        package_id = _id(package["package_id"], f"rules_packages[{index}].package_id")
        if package_id in seen_ids:
            raise ContractError("RULE_PACKAGE_DUPLICATE", f"duplicate package_id: {package_id}")
        seen_ids.add(package_id)
        release_id = _hash(package["release_id"], f"rules_packages[{index}].release_id")
        package_hash = _hash(package["package_hash"], f"rules_packages[{index}].package_hash")
        if release_id in seen_releases:
            raise ContractError("RULE_PACKAGE_DUPLICATE", f"duplicate release_id: {release_id}")
        seen_releases.add(release_id)
        version = _string(package["version"], f"rules_packages[{index}].version", maximum=64)
        normalized.append(
            {
                "package_id": package_id,
                "version": version,
                "release_id": release_id,
                "package_hash": package_hash,
                "rules": validate_rules(package["rules"]),
            }
        )
    return data, normalized


def _run_merge(
    request: Mapping[str, Any],
    host: HostPort,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    context = _context_for_request(request, capability=CAPABILITY_MERGE)
    try:
        data, packages = _validate_merge_request(request)
        if cancelled is not None and cancelled():
            _cancel_terminal(host, data, context, capability=CAPABILITY_MERGE)
            return None
        _event(host, context, "source-cleaning.started", None, 1)
        merged_rules: list[dict[str, Any]] = []
        seen_rule_ids: set[str] = set()
        package_refs: list[dict[str, Any]] = []
        for package_index, package in enumerate(packages):
            if cancelled is not None and cancelled():
                raise _Cancelled()
            package_refs.append(
                {
                    "package_id": package["package_id"],
                    "version": package["version"],
                    "release_id": package["release_id"],
                    "package_hash": package["package_hash"],
                }
            )
            for rule in package["rules"]["rules"]:
                if rule["id"] in seen_rule_ids:
                    raise ContractError("RULE_ID_DUPLICATE", f"duplicate rule id across packages: {rule['id']}")
                seen_rule_ids.add(rule["id"])
                adapted = deepcopy(rule)
                adapted["order"] = package_index * 1_000_000 + rule["order"]
                merged_rules.append(adapted)
        merged_rules.sort(key=lambda child: (child["order"], child["id"]))
        merged = {"schema": SOURCE_CLEANING_RULES_SCHEMA, "rules": merged_rules}
        payload = {
            "schema": SOURCE_CLEANING_MERGE_ARTIFACT_SCHEMA,
            "format_id": SOURCE_CLEANING_RULES_SCHEMA,
            "source_packages": package_refs,
            "rules": merged,
            "rules_hash": rules_hash(merged),
            "rule_order": [rule["id"] for rule in merged_rules],
            "merge_hash": _hash_jcs(
                SOURCE_CLEANING_MERGE_ARTIFACT_SCHEMA,
                {"source_packages": package_refs, "rules": merged},
            ),
        }
        source_refs = [
            {
                "workspace_id": data["workspace_id"],
                "source_type": "rules_package",
                "source_id": package["package_id"],
                "revision_or_hash": package["package_hash"],
            }
            for package in packages
        ]
        item = _artifact_item(
            data,
            context,
            capability=CAPABILITY_MERGE,
            payload=payload,
            artifact_kind="source-cleaning-rules-merged/v1",
            host=host,
            source_refs=source_refs,
        )
        _event(host, context, "source-cleaning.payload", item["payload_asset_id"], 2)
        _checkpoint(host, data, context, state_asset_id=item["payload_asset_id"])
        return _finalize(
            data,
            context,
            capability=CAPABILITY_MERGE,
            contract_id="artifact-bundle/v1",
            items=[item],
            host=host,
            stage_candidate=False,
            local_seq=3,
        )
    except _Cancelled:
        _cancel_terminal(host, request, context, capability=CAPABILITY_MERGE)
        return None
    except Exception as exc:
        _failure_terminal(host, request, context, capability=CAPABILITY_MERGE, error=exc)
        raise


def merge_rules(request: Mapping[str, Any], host: HostPort) -> dict[str, Any]:
    """Merge ordered rule packages into one artifact without emitting a Candidate."""

    result = _run_merge(request, host)
    if result is None:
        raise ContractError("CANCELLED", "source.clean.rules.merge/v1 was cancelled")
    return result


def _validate_cancel_request(request: Mapping[str, Any], *, capability: str) -> tuple[dict[str, Any], _RunContext]:
    required_keys = set(_CLEANING_COMMON_KEYS)
    if capability == CAPABILITY_APPLY:
        required_keys.add("review_context")
    data = _closed(request, required_keys, "cancel_request")
    if data["schema"] != f"{capability.replace('/v1', '')}-request/v1" or data["operation_key"] != capability or data["operation"] != "cancel":
        raise ContractError("OPERATION_INVALID", f"{capability} supports cancel only through operation=cancel")
    for field in ("job_id", "step_id", "attempt_id", "worker_run_id", "workspace_id", "document_id", "revision_id", "source_asset_id"):
        _id(data[field], f"cancel_request.{field}")
    for field in ("run_snapshot_hash", "base_content_hash", "source_asset_hash", "rules_release_id", "rules_package_hash"):
        _hash(data[field], f"cancel_request.{field}")
    _integer(data["lease_epoch"], "cancel_request.lease_epoch", minimum=1)
    _id(data["provenance_receipt_id"], "cancel_request.provenance_receipt_id")
    _string(data["created_at"], "cancel_request.created_at", maximum=64)
    _integer(data["total_units"], "cancel_request.total_units", minimum=0)
    if capability == CAPABILITY_APPLY:
        _validate_review(data["review_context"])
    context = _context_for_request(data, capability=capability)
    return data, context


def cancel(request: Mapping[str, Any], host: HostPort) -> None:
    """Signal the default worker owner, or terminalize only when no worker is active."""

    capability = request.get("operation_key") if isinstance(request, Mapping) else None
    if capability not in {CAPABILITY_PREVIEW, CAPABILITY_APPLY}:
        raise ContractError("CAPABILITY_INVALID", "only preview and apply expose cancellation")
    _DEFAULT_RUNTIME._cancel_request(request, host, capability=str(capability))


@dataclass
class _ActiveRun:
    request_hash: str
    capability: str
    cancel_requested: bool = False


@dataclass(frozen=True)
class _TerminalReservation:
    request_hash: str
    capability: str


@dataclass(frozen=True)
class _CompletedRun:
    request_hash: str
    outcome: str


class SourceCleaningRuntime:
    """B0-style facade where an active worker exclusively owns its terminal RPC."""

    def __init__(self) -> None:
        self._lock = allocate_lock()
        self._active: dict[str, _ActiveRun] = {}
        self._terminal_reservations: dict[str, _TerminalReservation] = {}
        self._completed: dict[str, _CompletedRun] = {}

    def _remember_completed(self, run_id: str, request_hash: str, outcome: str) -> None:
        self._completed[run_id] = _CompletedRun(request_hash, outcome)
        while len(self._completed) > 1024:
            self._completed.pop(next(iter(self._completed)))

    def cancel(self, run_id_or_request: str | Mapping[str, Any]) -> bool:
        run_id = run_id_or_request if isinstance(run_id_or_request, str) else str(run_id_or_request.get("worker_run_id") or "")
        if not run_id:
            return False
        with self._lock:
            active = self._active.get(run_id)
            if active is None:
                return False
            if not isinstance(run_id_or_request, str):
                capability = run_id_or_request.get("operation_key")
                if capability != active.capability:
                    raise ContractError("CANCEL_BINDING_INVALID", "cancel capability is not bound to the active worker run")
                cancel_hash = _context_for_request(run_id_or_request, capability=str(capability)).request_hash
                if cancel_hash != active.request_hash:
                    raise ContractError("CANCEL_BINDING_INVALID", "cancel request is not bound to the active worker run")
            was_new = not active.cancel_requested
            active.cancel_requested = True
            return was_new

    def _cancel_request(self, request: Mapping[str, Any], host: HostPort, *, capability: str) -> None:
        data, context = _validate_cancel_request(request, capability=capability)
        run_id = str(data["worker_run_id"])
        reservation = _TerminalReservation(context.request_hash, capability)
        with self._lock:
            active = self._active.get(run_id)
            if active is not None:
                if active.capability != capability or active.request_hash != context.request_hash:
                    raise ContractError("CANCEL_BINDING_INVALID", "cancel request is not bound to the active worker run")
                active.cancel_requested = True
                return
            existing_reservation = self._terminal_reservations.get(run_id)
            if existing_reservation is not None:
                if existing_reservation != reservation:
                    raise ContractError("CANCEL_BINDING_INVALID", "cancel request conflicts with the reserved terminal owner")
                return
            completed = self._completed.get(run_id)
            if completed is not None:
                if completed.request_hash == context.request_hash:
                    return
                del self._completed[run_id]
            self._terminal_reservations[run_id] = reservation
        try:
            _cancel_terminal(host, data, context, capability=capability)
        finally:
            with self._lock:
                if self._terminal_reservations.get(run_id) is reservation:
                    del self._terminal_reservations[run_id]
                self._remember_completed(run_id, context.request_hash, context.terminal_outcome or "failed")

    def run(self, request: Mapping[str, Any], host: HostPort, *, engine: Any | None = None) -> dict[str, Any] | None:
        capability = request.get("operation_key") if isinstance(request, Mapping) else None
        operation = request.get("operation") if isinstance(request, Mapping) else None
        if operation == "cancel":
            if capability not in {CAPABILITY_PREVIEW, CAPABILITY_APPLY}:
                raise ContractError("CAPABILITY_INVALID", "only preview and apply expose cancellation")
            self._cancel_request(request, host, capability=str(capability))
            return None
        if capability not in {CAPABILITY_PREVIEW, CAPABILITY_APPLY, CAPABILITY_MERGE}:
            raise ContractError("CAPABILITY_INVALID", "unknown source-cleaning capability")
        if capability == CAPABILITY_MERGE and operation not in {"run", "validate"}:
            raise ContractError("OPERATION_INVALID", "source.clean.rules.merge/v1 supports run and validate only")

        context = _context_for_request(request, capability=str(capability))
        run_id = str(request["worker_run_id"])
        control = _ActiveRun(context.request_hash, str(capability))
        with self._lock:
            reservation = self._terminal_reservations.get(run_id)
            if reservation is not None:
                raise ContractError("WORKER_RUN_TERMINAL_RESERVED", "worker_run_id has a reserved terminal owner")
            if run_id in self._active:
                raise ContractError("WORKER_RUN_ACTIVE", "worker_run_id already has an active owner")
            self._completed.pop(run_id, None)
            self._active[run_id] = control
        outcome = "failed"
        try:
            if capability == CAPABILITY_PREVIEW:
                result = _run_preview(request, host, engine=engine, cancelled=lambda: self._is_cancelled(run_id, control))
            elif capability == CAPABILITY_APPLY:
                result = _run_apply(request, host, engine=engine, cancelled=lambda: self._is_cancelled(run_id, control))
            else:
                result = _run_merge(request, host, cancelled=lambda: self._is_cancelled(run_id, control))
            outcome = "cancelled" if result is None else "succeeded"
            return result
        finally:
            with self._lock:
                if self._active.get(run_id) is control:
                    del self._active[run_id]
                self._remember_completed(run_id, context.request_hash, outcome)

    def _is_cancelled(self, run_id: str, control: _ActiveRun) -> bool:
        with self._lock:
            return self._active.get(run_id) is control and control.cancel_requested


_DEFAULT_RUNTIME = SourceCleaningRuntime()


def make_cleaning_request(
    *,
    capability: str,
    operation: str = "run",
    job_id: str,
    step_id: str,
    attempt_id: str,
    lease_epoch: int,
    run_snapshot_hash: str,
    workspace_id: str,
    document_id: str,
    revision_id: str,
    base_content_hash: str | None = None,
    source_asset_id: str,
    rules: Mapping[str, Any],
    rules_release_id: str,
    rules_package_hash: str,
    input_kind: str,
    storage_source_format: str,
    body_ranges: Sequence[Mapping[str, int]] | None = None,
    title_ranges: Sequence[Mapping[str, int]] | None = None,
    # ``source_text`` is a test/helper convenience for calculating the
    # expected immutable Asset hash.  It is intentionally never emitted in
    # the Core request; production callers may provide source_asset_hash
    # instead.
    source_text: str | None = None,
    source_asset_hash: str | None = None,
    thresholds: Mapping[str, Any] | None = None,
    excluded_hit_ids: Sequence[str] | None = None,
    limits: Mapping[str, Any] | None = None,
    review_context: Mapping[str, Any] | None = None,
    worker_run_id: str | None = None,
    checkpoint_ids: Sequence[str] | None = None,
    provenance_receipt_id: str | None = None,
    created_at: str = _FIXED_TIME,
    total_units: int = 1,
    resume_checkpoint_asset_id: str | None = None,
    resume_checkpoint_asset_hash: str | None = None,
    resume_state_asset_id: str | None = None,
    resume_state_asset_hash: str | None = None,
) -> dict[str, Any]:
    if capability not in {CAPABILITY_PREVIEW, CAPABILITY_APPLY}:
        raise ContractError("CAPABILITY_INVALID", "make_cleaning_request supports preview or apply only")
    normalized = validate_rules(rules)
    profile = build_profile(
        normalized,
        rules_release_id=rules_release_id,
        rules_package_hash=rules_package_hash,
        thresholds=thresholds,
        excluded_hit_ids=excluded_hit_ids,
    )
    if source_text is not None:
        calculated_source_hash = sha256_text(source_text)
    elif source_asset_hash is not None:
        calculated_source_hash = _hash(source_asset_hash, "source_asset_hash")
    else:
        raise ContractError("SOURCE_HASH_REQUIRED", "make_cleaning_request needs source_asset_hash when no helper text is supplied")
    if base_content_hash is None:
        base_content_hash = calculated_source_hash
    if operation == "resume":
        resume_values = (resume_checkpoint_asset_id, resume_checkpoint_asset_hash, resume_state_asset_id, resume_state_asset_hash)
        if any(value is None for value in resume_values):
            raise ContractError("RESUME_FIELDS_INVALID", "resume requires checkpoint/state Asset IDs and hashes")
    elif any(value is not None for value in (resume_checkpoint_asset_id, resume_checkpoint_asset_hash, resume_state_asset_id, resume_state_asset_hash)):
        raise ContractError("RESUME_FIELDS_INVALID", "resume Asset bindings require operation=resume")
    request: dict[str, Any] = {
        "schema": f"{capability.replace('/v1', '')}-request/v1",
        "operation_key": capability,
        "operation": operation,
        "job_id": job_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "worker_run_id": worker_run_id or f"worker-{attempt_id}",
        "lease_epoch": lease_epoch,
        "checkpoint_ids": list(checkpoint_ids or [f"checkpoint-{attempt_id}-1"]),
        "provenance_receipt_id": provenance_receipt_id or f"receipt-{attempt_id}",
        "created_at": created_at,
        "total_units": total_units,
        "run_snapshot_hash": run_snapshot_hash,
        "workspace_id": workspace_id,
        "document_id": document_id,
        "revision_id": revision_id,
        "base_content_hash": base_content_hash,
        "source_asset_id": source_asset_id,
        "source_asset_hash": calculated_source_hash,
        "storage_source_format": storage_source_format,
        "input_kind": input_kind,
        "rules": normalized,
        "rules_release_id": rules_release_id,
        "rules_package_hash": rules_package_hash,
        "profile": profile,
        "limits": dict(DEFAULT_LIMITS if limits is None else limits),
        "body_ranges": list(body_ranges or []),
        "title_ranges": list(title_ranges or []),
    }
    if capability == CAPABILITY_APPLY:
        if review_context is None:
            raise ContractError("REVIEW_CONTEXT_REQUIRED", "apply requires a preview-derived review_context")
        request["review_context"] = deepcopy(dict(review_context))
    if operation == "resume":
        request.update(
            {
                "resume_checkpoint_asset_id": resume_checkpoint_asset_id,
                "resume_checkpoint_asset_hash": resume_checkpoint_asset_hash,
                "resume_state_asset_id": resume_state_asset_id,
                "resume_state_asset_hash": resume_state_asset_hash,
            }
        )
    return request


def make_merge_request(
    *,
    job_id: str,
    step_id: str,
    attempt_id: str,
    lease_epoch: int,
    run_snapshot_hash: str,
    workspace_id: str,
    rules_packages: Sequence[Mapping[str, Any]],
    operation: str = "run",
    worker_run_id: str | None = None,
    checkpoint_ids: Sequence[str] | None = None,
    provenance_receipt_id: str | None = None,
    created_at: str = _FIXED_TIME,
    total_units: int = 1,
) -> dict[str, Any]:
    return {
        "schema": "source.clean.rules.merge-request/v1",
        "operation_key": CAPABILITY_MERGE,
        "operation": operation,
        "job_id": job_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "worker_run_id": worker_run_id or f"worker-{attempt_id}",
        "lease_epoch": lease_epoch,
        "checkpoint_ids": list(checkpoint_ids or [f"checkpoint-{attempt_id}-1"]),
        "provenance_receipt_id": provenance_receipt_id or f"receipt-{attempt_id}",
        "created_at": created_at,
        "total_units": total_units,
        "run_snapshot_hash": run_snapshot_hash,
        "workspace_id": workspace_id,
        "rules_packages": deepcopy(list(rules_packages)),
    }


def dispatch(request: Mapping[str, Any], host: Any, *, engine: Any | None = None) -> dict[str, Any] | None:
    return _DEFAULT_RUNTIME.run(request, host, engine=engine)


def capability_descriptor(capability_id: str | None = None) -> dict[str, Any]:
    """Return the same descriptor shape used by the accepted B0 loader."""

    identity = load_identity()
    descriptors = {
        CAPABILITY_PREVIEW: {
            "schema": "capability-provider/v1",
            "capability_id": CAPABILITY_PREVIEW,
            "provider": {"plugin_id": PLUGIN_ID, "release_id": identity["release_id"]},
            "input_schema": "source.clean.preview-request/v1",
            "output_schema": "source.clean.preview-result/v1",
            "result_contract": "artifact-bundle/v1",
            "supports": ["run", "cancel"],
            "deterministic": True,
            "accepted_data_formats": ["source-cleaning-rules/v1"],
        },
        CAPABILITY_APPLY: {
            "schema": "capability-provider/v1",
            "capability_id": CAPABILITY_APPLY,
            "provider": {"plugin_id": PLUGIN_ID, "release_id": identity["release_id"]},
            "input_schema": "source.clean.apply-request/v1",
            "output_schema": "source.clean.apply-result/v1",
            "result_contract": "candidate-batch/v1",
            "supports": ["run", "resume", "cancel"],
            "deterministic": True,
            "accepted_data_formats": ["source-cleaning-rules/v1"],
        },
        CAPABILITY_MERGE: {
            "schema": "capability-provider/v1",
            "capability_id": CAPABILITY_MERGE,
            "provider": {"plugin_id": PLUGIN_ID, "release_id": identity["release_id"]},
            "input_schema": "source.clean.rules.merge-request/v1",
            "output_schema": "source.clean.rules.merge-result/v1",
            "result_contract": "artifact-bundle/v1",
            "supports": ["run", "validate"],
            "deterministic": True,
            "accepted_data_formats": ["source-cleaning-rules/v1"],
        },
    }
    selected = CAPABILITY_PREVIEW if capability_id is None else capability_id
    if selected not in descriptors:
        raise ContractError("CAPABILITY_INVALID", "unknown source-cleaning capability descriptor")
    return descriptors[selected]


def main(request: Mapping[str, Any] | None = None, host: Any | None = None, *, engine: Any | None = None) -> dict[str, Any] | None:
    """Accepted B0 loader entrypoint: ``main(request, host)``."""

    if request is None:
        return capability_descriptor()
    if host is None:
        raise HostBindingError("source-cleaning entrypoint requires HostPort.call(method, params)")
    return dispatch(request, host, engine=engine)


__all__ = [
    "apply",
    "build_review_context",
    "capability_descriptor",
    "cancel",
    "dispatch",
    "main",
    "make_cleaning_request",
    "make_merge_request",
    "merge_rules",
    "preview",
    "read_asset_json",
    "regex_runtime_status",
    "SourceCleaningRuntime",
]
