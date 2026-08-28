"""Generated package identity reader for the source-cleaning code bundle."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.source-cleaning-runtime"
VERSION = "0.1.0"
MODULE_NAME = "source_cleaning_runtime"
IDENTITY_SCHEMA = "source-plugin-package-identity/v1"
_FIELDS = {"schema", "plugin_id", "version", "package_hash", "release_id"}


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_identity(root: Path | None = None) -> dict[str, str]:
    try:
        if root is not None:
            raw = (root / MODULE_NAME / "identity.json").read_bytes()
        else:
            # ``Path(__file__)`` is a virtual ``.whl/...`` path under
            # zipimport and cannot be opened by pathlib.  importlib.resources
            # reads the same sidecar from either a directory or a wheel.
            raw = resources.files(MODULE_NAME).joinpath("identity.json").read_bytes()
        value: Any = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("source-cleaning package identity artifact is unavailable") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise RuntimeError("source-cleaning package identity schema is invalid")
    if value["schema"] != IDENTITY_SCHEMA or value["plugin_id"] != PLUGIN_ID or value["version"] != VERSION:
        raise RuntimeError("source-cleaning package identity owner/version is invalid")
    for field in ("package_hash", "release_id"):
        raw = value[field]
        if not isinstance(raw, str) or len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
            raise RuntimeError(f"source-cleaning package identity field is invalid: {field}")
    return {key: str(value[key]) for key in value}


__all__ = ["IDENTITY_SCHEMA", "MODULE_NAME", "PLUGIN_ID", "VERSION", "load_identity", "package_root"]
