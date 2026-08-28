"""Manifest-bound source-import package identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.source-import"
VERSION = "0.1.0"
MODULE_NAME = "source_import"
IDENTITY_SCHEMA = "source-import-package-identity/v1"
_EXPECTED_FIELDS = {"schema", "plugin_id", "version", "package_hash", "release_id"}
_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (.+)\n$")


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def identity_path(root: Path | None = None) -> Path:
    return (root or package_root()) / MODULE_NAME / "identity.json"


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(f"source-import package identity has duplicate key: {key}")
        value[key] = item
    return value


def _read_identity(root: Path | None = None) -> dict[str, Any]:
    path = identity_path(root)
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError("source-import package identity must not have a UTF-8 BOM")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError("source-import package identity is unavailable") from exc
    if not isinstance(value, dict) or set(value) != _EXPECTED_FIELDS:
        raise RuntimeError("source-import package identity fields are invalid")
    if value["schema"] != IDENTITY_SCHEMA or value["plugin_id"] != PLUGIN_ID or value["version"] != VERSION:
        raise RuntimeError("source-import package identity owner/version is invalid")
    for field in ("package_hash", "release_id"):
        raw = value[field]
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{64}", raw):
            raise RuntimeError(f"source-import identity {field} is invalid")
    return value


def _manifest_paths(root: Path) -> tuple[tuple[str, ...], bytes]:
    path = root / "files.sha256"
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("source-import files.sha256 is unavailable") from exc
    if raw.startswith(b"\xef\xbb\xbf") or not raw or b"\r" in raw or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise RuntimeError("source-import files.sha256 must be UTF-8 LF with one final newline")
    names: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError("source-import files.sha256 line is malformed")
        name = match.group(2)
        if name == "files.sha256" or name in names:
            raise RuntimeError("source-import manifest has a duplicate/self entry")
        names.append(name)
    if not names:
        raise RuntimeError("source-import manifest is empty")
    return tuple(names), raw


def canonical_payload_files(root: Path | None = None) -> dict[str, bytes]:
    bundle_root = root or package_root()
    names, _ = _manifest_paths(bundle_root)
    result: dict[str, bytes] = {}
    resolved_root = bundle_root.resolve()
    for name in names:
        path = bundle_root / name
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(f"source-import manifest path escapes bundle: {name}") from exc
        if not path.is_file():
            raise RuntimeError(f"source-import manifest member is missing: {name}")
        result[name] = path.read_bytes()
    return result


def calculate_identity(root: Path | None = None):
    bundle_root = root or package_root()
    value = _read_identity(bundle_root)
    try:
        from plotpilot_plugin_sdk.package import digest_package
    except ImportError as exc:
        raise RuntimeError("public PlotPilot SDK is unavailable") from exc
    names, manifest = _manifest_paths(bundle_root)
    files = canonical_payload_files(bundle_root)
    if tuple(files) != names:
        raise RuntimeError("source-import manifest order changed")
    digest = digest_package(files, PLUGIN_ID, VERSION)
    if digest.files_sha256 != manifest or value["package_hash"] != digest.package_hash or value["release_id"] != digest.release_id:
        raise RuntimeError("source-import package identity does not match its canonical payload")
    return digest


def load_runtime_identity() -> dict[str, Any]:
    value = _read_identity()
    manifest = package_root() / "files.sha256"
    if manifest.is_file():
        try:
            calculate_identity()
        except ModuleNotFoundError as exc:
            if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"):
                raise
        except RuntimeError as exc:
            if "public PlotPilot SDK is unavailable" not in str(exc):
                raise
    return value
