"""RFC 8785 JSON Canonicalization and content hashing helpers."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable

import rfc8785

from .errors import ContractValidationError


def _reject_constant(value: str) -> None:
    raise ContractValidationError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def parse_json_bytes(data: bytes) -> Any:
    """Parse UTF-8 JSON while rejecting BOMs, duplicate keys and NaN values."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractValidationError("UTF-8 BOM is forbidden")
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_reject_duplicate, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"invalid UTF-8 JSON: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    """Return RFC 8785 bytes and fail closed for values outside JSON."""
    try:
        return bytes(rfc8785.dumps(value))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractValidationError(f"value is not RFC 8785 JSON: {exc}") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_jcs(prefix: str, value: Any) -> str:
    return sha256_hex(prefix.encode("ascii") + b"\n" + canonical_bytes(value))


def without(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = deepcopy(value)
    result.pop(field, None)
    return result


def normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize only the set-like arrays listed in §84.1.

    Ordered Plan/Data/Skill arrays are deliberately left untouched.  This
    function is shared by snapshot hashing and the cross-language golden test.
    """
    result = deepcopy(snapshot)
    sort_keys: dict[str, Callable[[dict[str, Any]], tuple[Any, ...]]] = {
        "input_revisions": lambda x: (x.get("document_id", ""), x.get("revision_id", "")),
        "plugin_releases": lambda x: (x.get("plugin_id", ""),),
        "plugin_settings_revisions": lambda x: (x.get("plugin_id", ""), x.get("scope", ""), x.get("scope_id") or ""),
        "asset_hashes": lambda x: (x.get("asset_id", ""),),
    }
    for field, key in sort_keys.items():
        if isinstance(result.get(field), list):
            result[field] = sorted(result[field], key=key)
    return result
