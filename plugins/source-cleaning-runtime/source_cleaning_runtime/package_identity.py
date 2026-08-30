"""Generated package identity reader for the source-cleaning code bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.source-cleaning-runtime"
VERSION = "0.1.0"
MODULE_NAME = "source_cleaning_runtime"
IDENTITY_SCHEMA = "source-plugin-package-identity/v1"
_FIELDS = {"schema", "plugin_id", "version", "package_hash", "release_id"}
_GENERATED_OUTER_FILES = {
    ".gitattributes",
    "descriptor.json",
    "descriptor-apply.json",
    "descriptor-merge.json",
    "expected.json",
    "files.sha256",
    f"{MODULE_NAME}/identity.json",
}


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _manifest_entries(raw: bytes) -> list[tuple[str, str]]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuntimeError("source-cleaning package manifest is not UTF-8") from exc
    if not text or not text.endswith("\n") or "\r" in text:
        raise RuntimeError("source-cleaning package manifest is not canonical")
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if len(line) < 67 or line[64:66] != "  ":
            raise RuntimeError("source-cleaning package manifest row is invalid")
        digest, relative = line[:64], line[66:]
        if any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeError("source-cleaning package manifest digest is invalid")
        path = Path(relative)
        if (
            not relative
            or "\\" in relative
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != relative
        ):
            raise RuntimeError("source-cleaning package manifest path is invalid")
        entries.append((relative, digest))
    paths = [relative for relative, _ in entries]
    if paths != sorted(paths, key=lambda value: value.encode("utf-8")) or len(paths) != len(set(paths)):
        raise RuntimeError("source-cleaning package manifest order is invalid")
    canonical = "".join(f"{digest}  {relative}\n" for relative, digest in entries).encode("utf-8")
    if canonical != raw:
        raise RuntimeError("source-cleaning package manifest is not canonical")
    return entries


def _payload_paths(root: Path) -> set[str]:
    result: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        if relative in _GENERATED_OUTER_FILES:
            continue
        result.add(relative)
    return result


def _verified_outer_identity(root: Path, manifest_raw: bytes) -> tuple[str, str]:
    entries = _manifest_entries(manifest_raw)
    listed = {relative for relative, _ in entries}
    if listed != _payload_paths(root):
        raise RuntimeError("source-cleaning package payload set does not match files.sha256")
    resolved_root = root.resolve()
    for relative, expected in entries:
        path = root / Path(relative)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root) or path.is_symlink() or not path.is_file():
                raise OSError(relative)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f"source-cleaning package payload is unavailable: {relative}") from exc
        if actual != expected:
            raise RuntimeError(f"source-cleaning package payload hash mismatch: {relative}")
    package_hash = hashlib.sha256(b"plotpilot-package/v1\n" + manifest_raw).hexdigest()
    release_id = hashlib.sha256(
        f"plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n".encode("utf-8")
    ).hexdigest()
    return package_hash, release_id


def load_identity(root: Path | None = None) -> dict[str, str]:
    package = package_root() if root is None else Path(root)
    try:
        raw = (package / MODULE_NAME / "identity.json").read_bytes()
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
    try:
        manifest_raw = (package / "files.sha256").read_bytes()
    except OSError as exc:
        raise RuntimeError("source-cleaning package manifest is unavailable") from exc
    package_hash, release_id = _verified_outer_identity(package, manifest_raw)
    if value["package_hash"] != package_hash or value["release_id"] != release_id:
        raise RuntimeError("source-cleaning package identity does not match verified files.sha256")
    return {key: str(value[key]) for key in value}


__all__ = ["IDENTITY_SCHEMA", "MODULE_NAME", "PLUGIN_ID", "VERSION", "load_identity", "package_root"]
