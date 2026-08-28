"""Provider package identity helpers using only the public SDK digest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

PLUGIN_ID = "com.plotpilot.novelagent.provider-gemini"
VERSION = "0.1.0"
IDENTITY_SCHEMA = "provider-package-identity/v1"


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def identity_path() -> Path:
    return Path(__file__).with_name("identity.json")


def load_runtime_identity() -> dict[str, Any]:
    try:
        value = json.loads(identity_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("provider package identity artifact is unavailable") from exc
    if not isinstance(value, dict) or value.get("schema") != IDENTITY_SCHEMA:
        raise RuntimeError("provider package identity schema is invalid")
    if value.get("plugin_id") != PLUGIN_ID or value.get("version") != VERSION:
        raise RuntimeError("provider package identity owner/version is invalid")
    for field in ("package_hash", "release_id"):
        raw = value.get(field)
        if not isinstance(raw, str) or len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
            raise RuntimeError(f"provider package identity field is invalid: {field}")
    identity_files = value.get("identity_files")
    if not isinstance(identity_files, list) or not identity_files or not all(isinstance(item, str) for item in identity_files):
        raise RuntimeError("provider identity_files must be a non-empty string list")
    return value


def identity_input_files(root: Path | None = None, *, identity: Mapping[str, Any] | None = None) -> dict[str, bytes]:
    root = root or package_root()
    identity = identity or load_runtime_identity()
    names = identity.get("identity_files")
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        raise RuntimeError("provider identity_files is invalid")
    result: dict[str, bytes] = {}
    for name in names:
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"provider identity input is missing: {name}")
        result[name] = path.read_bytes()
    return result


def calculate_identity(root: Path | None = None) -> Any:
    from plotpilot_plugin_sdk.package import digest_package

    identity = load_runtime_identity()
    return digest_package(identity_input_files(root, identity=identity), PLUGIN_ID, VERSION)
