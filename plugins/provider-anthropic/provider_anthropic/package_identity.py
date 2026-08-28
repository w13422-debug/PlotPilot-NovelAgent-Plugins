"""Provider package identity helpers bound to the committed canonical payload manifest.

The generated identity sidecar is deliberately outside the hashed payload.  The
canonical payload map is not a second file list: it is derived from the exact
paths committed in ``files.sha256`` and passed unchanged to the public SDK
``digest_package`` implementation.  The installable wheel is one of those
manifest entries, while this sidecar and the descriptor are derived metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.provider-anthropic"
VERSION = "0.1.0"
MODULE_NAME = "provider_anthropic"
IDENTITY_SCHEMA = "provider-package-identity/v1"
_EXPECTED_FIELDS = {"schema", "plugin_id", "version", "package_hash", "release_id"}
_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (.+)\n$")


def package_root() -> Path:
    """Return the plugin bundle root in a source checkout or extracted wheel."""
    return Path(__file__).resolve().parents[1]


def identity_path(root: Path | None = None) -> Path:
    """Return the generated identity sidecar, never a digest input."""
    bundle_root = root or package_root()
    return bundle_root / MODULE_NAME / "identity.json"


def _read_identity(root: Path | None = None) -> dict[str, Any]:
    path = identity_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("provider package identity artifact is unavailable") from exc
    if not isinstance(value, dict) or set(value) != _EXPECTED_FIELDS:
        raise RuntimeError("provider package identity schema is invalid")
    if value.get("schema") != IDENTITY_SCHEMA or value.get("plugin_id") != PLUGIN_ID or value.get("version") != VERSION:
        raise RuntimeError("provider package identity owner/version is invalid")
    for field in ("package_hash", "release_id"):
        raw = value.get(field)
        if not isinstance(raw, str) or len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
            raise RuntimeError(f"provider package identity field is invalid: {field}")
    return value


def _manifest_paths(root: Path) -> tuple[tuple[str, ...], bytes]:
    path = root / "files.sha256"
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("provider package manifest is unavailable or not UTF-8") from exc
    if raw.startswith(b"\xef\xbb\xbf") or not raw or b"\r" in raw or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise RuntimeError("provider package manifest must be UTF-8 LF with one final newline")
    paths: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError("provider package manifest line is malformed")
        relative = match.group(2)
        if relative == "files.sha256" or relative in paths:
            raise RuntimeError("provider package manifest contains a duplicate or self entry")
        paths.append(relative)
    if not paths:
        raise RuntimeError("provider package manifest is empty")
    return tuple(paths), raw


def canonical_payload_files(root: Path | None = None) -> dict[str, bytes]:
    """Load exactly the file map represented by ``files.sha256``."""
    bundle_root = root or package_root()
    names, _ = _manifest_paths(bundle_root)
    resolved_root = bundle_root.resolve()
    result: dict[str, bytes] = {}
    for name in names:
        path = bundle_root / name
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(f"provider package manifest path escapes bundle: {name}") from exc
        if not path.is_file():
            raise RuntimeError(f"provider package manifest input is missing: {name}")
        result[name] = path.read_bytes()
    return result


def _digest_from_manifest(root: Path):
    from plotpilot_plugin_sdk.package import digest_package

    names, manifest_bytes = _manifest_paths(root)
    files = canonical_payload_files(root)
    if tuple(files) != names:
        raise RuntimeError("provider canonical payload map does not preserve manifest order")
    digest = digest_package(files, PLUGIN_ID, VERSION)
    if digest.files_sha256 != manifest_bytes:
        raise RuntimeError("provider committed files.sha256 does not match the public SDK digest")
    return digest


def load_runtime_identity() -> dict[str, Any]:
    """Load the sidecar and verify it against the manifest when present."""
    value = _read_identity()
    manifest_path = package_root() / "files.sha256"
    if manifest_path.is_file():
        try:
            digest = _digest_from_manifest(package_root())
        except ModuleNotFoundError as exc:
            if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"):
                raise
            # Importing a provider must remain possible for its fail-closed
            # SDK_UNAVAILABLE path; no alternate digest implementation is used.
            return value
        if value["package_hash"] != digest.package_hash or value["release_id"] != digest.release_id:
            raise RuntimeError("provider package identity does not match the canonical payload")
    return value


def calculate_identity(root: Path | None = None):
    """Calculate and verify identity with the public SDK over the manifest map."""
    bundle_root = root or package_root()
    value = _read_identity(bundle_root)
    digest = _digest_from_manifest(bundle_root)
    if value["package_hash"] != digest.package_hash or value["release_id"] != digest.release_id:
        raise RuntimeError("provider package identity does not match the canonical payload")
    return digest
