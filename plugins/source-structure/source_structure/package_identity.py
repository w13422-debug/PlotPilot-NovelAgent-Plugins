"""Package identity verifier for the generated source-structure bundle."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

PLUGIN_ID = "com.plotpilot.novelagent.source-structure"
VERSION = "0.1.0"
MODULE_NAME = "source_structure"
IDENTITY_SCHEMA = "source-structure-package-identity/v1"
_FIELDS = frozenset({"schema","plugin_id","version","package_hash","release_id"})
_LINE = re.compile(r"^[0-9a-f]{64}  (.+)\n$")


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def identity_path(root: Path|None = None) -> Path:
    return (root or package_root()) / MODULE_NAME / "identity.json"


def _read_identity(root: Path|None = None) -> dict[str,Any]:
    try: value = json.loads(identity_path(root).read_text(encoding="utf-8"))
    except Exception as exc: raise RuntimeError("source-structure package identity is unavailable") from exc
    if not isinstance(value,dict) or set(value) != set(_FIELDS): raise RuntimeError("source-structure package identity schema is invalid")
    if value["schema"] != IDENTITY_SCHEMA or value["plugin_id"] != PLUGIN_ID or value["version"] != VERSION: raise RuntimeError("source-structure package identity owner/version is invalid")
    for field in ("package_hash","release_id"):
        if not isinstance(value[field],str) or re.fullmatch(r"[0-9a-f]{64}",value[field]) is None: raise RuntimeError("source-structure package identity hash is invalid")
    return value


def _manifest(root: Path) -> tuple[tuple[str,...],bytes]:
    raw = (root/"files.sha256").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n") or raw.endswith(b"\n\n"): raise RuntimeError("source-structure manifest must be UTF-8 LF with one final newline")
    names:list[str] = []
    for line in raw.decode("utf-8").splitlines(keepends=True):
        match = _LINE.fullmatch(line)
        if match is None: raise RuntimeError("source-structure manifest line is malformed")
        name = match.group(1)
        if name in names or name == "files.sha256": raise RuntimeError("source-structure manifest has duplicate/self entry")
        names.append(name)
    if not names: raise RuntimeError("source-structure manifest is empty")
    return tuple(names),raw


def canonical_payload_files(root: Path|None = None) -> dict[str,bytes]:
    bundle = root or package_root()
    names,_ = _manifest(bundle)
    resolved = bundle.resolve()
    result:dict[str,bytes] = {}
    for name in names:
        path = bundle/name
        try: path.resolve().relative_to(resolved)
        except ValueError as exc: raise RuntimeError("source-structure manifest escapes package") from exc
        if not path.is_file(): raise RuntimeError("source-structure manifest input is missing: "+name)
        result[name] = path.read_bytes()
    return result


def calculate_identity(root: Path|None = None):
    from plotpilot_plugin_sdk.package import digest_package
    bundle = root or package_root()
    value = _read_identity(bundle)
    names,manifest = _manifest(bundle)
    files = canonical_payload_files(bundle)
    if tuple(files) != names: raise RuntimeError("source-structure manifest order mismatch")
    digest = digest_package(files,PLUGIN_ID,VERSION)
    if digest.files_sha256 != manifest: raise RuntimeError("source-structure files.sha256 does not match SDK digest")
    if value["package_hash"] != digest.package_hash or value["release_id"] != digest.release_id: raise RuntimeError("source-structure identity does not match SDK digest")
    return digest


def load_runtime_identity() -> dict[str,Any]:
    value = _read_identity()
    manifest = package_root()/"files.sha256"
    if manifest.is_file():
        try: calculate_identity(package_root())
        except ModuleNotFoundError as exc:
            if not exc.name or not exc.name.startswith("plotpilot_plugin_sdk"): raise
    return value