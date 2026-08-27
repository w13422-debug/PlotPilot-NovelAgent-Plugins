"""Frozen §13.3/§13.4 package and Skill digest implementation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .canonical import sha256_hex
from .errors import ContractError, ErrorCode
from ._workspace import WORKSPACE_ROOT


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = {"con", "prn", "aux", "nul", "clock$", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_FORBIDDEN_CHARS = set('<>"|?*:\x00')
_CASEFOLD_TABLE_PATH = WORKSPACE_ROOT / "contracts" / "unicode-casefold-v1.json"
_HANGUL_PROFILE = {
    "s_base": 0xAC00,
    "l_base": 0x1100,
    "v_base": 0x1161,
    "t_base": 0x11A7,
    "l_count": 19,
    "v_count": 21,
    "t_count": 28,
}


def _load_unicode_contract() -> tuple[dict[str, str], dict[int, tuple[int, ...]], dict[int, int], dict[tuple[int, int], int]]:
    payload = json.loads(_CASEFOLD_TABLE_PATH.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "unicode-casefold/v1"
        or payload.get("unicode_data_version") != "15.0.0"
        or payload.get("nfc_hangul") != _HANGUL_PROFILE
    ):
        raise RuntimeError("invalid Unicode casefold contract version")
    mappings = payload.get("mappings")
    decomposition = payload.get("nfc_decomposition")
    combining_class = payload.get("nfc_combining_class")
    composition = payload.get("nfc_composition")
    if not isinstance(mappings, dict) or not isinstance(decomposition, dict) or not isinstance(combining_class, dict) or not isinstance(composition, dict):
        raise RuntimeError("Unicode casefold contract mappings must be an object")
    table: dict[str, str] = {}
    for raw_codepoint, folded in mappings.items():
        if not isinstance(raw_codepoint, str) or not isinstance(folded, str):
            raise RuntimeError("Unicode casefold contract mapping has invalid types")
        codepoint = int(raw_codepoint, 16)
        if not 0 <= codepoint <= 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise RuntimeError("Unicode casefold contract contains an invalid code point")
        table[chr(codepoint)] = folded
    nfc_decomposition = {
        int(raw_codepoint, 16): tuple(parts)
        for raw_codepoint, parts in decomposition.items()
        if isinstance(raw_codepoint, str) and isinstance(parts, list) and all(isinstance(part, int) for part in parts)
    }
    nfc_combining_class = {
        int(raw_codepoint, 16): value
        for raw_codepoint, value in combining_class.items()
        if isinstance(raw_codepoint, str) and isinstance(value, int)
    }
    nfc_composition: dict[tuple[int, int], int] = {}
    for raw_pair, value in composition.items():
        if not isinstance(raw_pair, str) or not isinstance(value, int) or "+" not in raw_pair:
            raise RuntimeError("Unicode NFC composition contract has invalid types")
        left, right = raw_pair.split("+", 1)
        nfc_composition[(int(left, 16), int(right, 16))] = value
    return table, nfc_decomposition, nfc_combining_class, nfc_composition


_CASEFOLD_TABLE, _NFC_DECOMPOSITION, _NFC_COMBINING_CLASS, _NFC_COMPOSITION = _load_unicode_contract()


def _unicode_nfc(value: str) -> str:
    decomposed: list[int] = []

    def append_decomposed(codepoint: int) -> None:
        parts = _NFC_DECOMPOSITION.get(codepoint)
        if parts is None:
            decomposed.append(codepoint)
            return
        for part in parts:
            append_decomposed(part)

    for character in value:
        append_decomposed(ord(character))

    # Canonical ordering is stable and never crosses a starter (combining
    # class zero).  Insertion is bounded by the current combining segment.
    ordered: list[int] = []
    for codepoint in decomposed:
        ccc = _NFC_COMBINING_CLASS.get(codepoint, 0)
        if ccc == 0:
            ordered.append(codepoint)
            continue
        index = len(ordered)
        while index > 0 and _NFC_COMBINING_CLASS.get(ordered[index - 1], 0) > ccc:
            index -= 1
        ordered.insert(index, codepoint)

    composed: list[int] = []
    starter_index = -1
    last_ccc = 0
    for codepoint in ordered:
        ccc = _NFC_COMBINING_CLASS.get(codepoint, 0)
        composite = None
        if starter_index >= 0 and (last_ccc == 0 or last_ccc < ccc):
            composite = _NFC_COMPOSITION.get((composed[starter_index], codepoint))
        if composite is not None:
            composed[starter_index] = composite
            # The mark was absorbed into the starter.  It must not become
            # the blocking class for the next canonical composition; the
            # previous unabsorbed mark still determines that boundary.
        else:
            if ccc == 0:
                starter_index = len(composed)
            composed.append(codepoint)
            last_ccc = ccc
    return "".join(chr(codepoint) for codepoint in composed)


def unicode_nfc_casefold(value: str) -> str:
    """Apply the frozen UCD 15.0.0 NFC/full-casefold path identity rule."""
    if not isinstance(value, str):
        raise TypeError("casefold identity requires a string")
    normalized = _unicode_nfc(value)
    return "".join(_CASEFOLD_TABLE.get(character, character) for character in normalized)


def normalize_relative_path(path: str) -> str:
    """Validate the Windows package path profile without silently trimming."""
    if not isinstance(path, str) or not path:
        raise ContractError(ErrorCode.ASSET_ERROR, "path must be a non-empty string")
    if "\\" in path or "\x00" in path:
        raise ContractError(ErrorCode.ASSET_ERROR, "backslash/NUL is forbidden in package paths", path=path)
    normalized = _unicode_nfc(path)
    if normalized.startswith("/") or normalized.startswith("//") or _DRIVE.match(normalized) or normalized.startswith("\\\\?\\"):
        raise ContractError(ErrorCode.ASSET_ERROR, "absolute, drive, UNC or extended path is forbidden", path=path)
    segments = normalized.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ContractError(ErrorCode.ASSET_ERROR, "empty/dot traversal segment is forbidden", path=path)
    if len(normalized) > 240:
        raise ContractError(ErrorCode.ASSET_ERROR, "relative path exceeds 240 Unicode scalar values", path=path)
    for segment in segments:
        if segment[-1] in ". ":
            raise ContractError(ErrorCode.ASSET_ERROR, "trailing dot/space is forbidden", path=path)
        if any(char in _FORBIDDEN_CHARS or ord(char) < 32 for char in segment):
            raise ContractError(ErrorCode.ASSET_ERROR, "forbidden Windows path character", path=path)
        stem = unicode_nfc_casefold(segment.split(".", 1)[0])
        if stem in _RESERVED:
            raise ContractError(ErrorCode.ASSET_ERROR, "Windows reserved device name is forbidden", path=path)
    return normalized


def _normalized_file_map(files: Mapping[str, bytes]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for raw_path, content in files.items():
        path = normalize_relative_path(raw_path)
        if path == "files.sha256":
            raise ContractError(ErrorCode.ASSET_ERROR, "files.sha256 is generated and cannot be an input file")
        if not isinstance(content, bytes):
            raise ContractError(ErrorCode.ASSET_ERROR, "package content must be raw bytes", path=path)
        key = unicode_nfc_casefold(path)
        if key in {unicode_nfc_casefold(existing) for existing in normalized}:
            raise ContractError(ErrorCode.ASSET_ERROR, "casefold path collision", path=path)
        normalized[path] = content
    return normalized


def build_files_sha256(files: Mapping[str, bytes]) -> bytes:
    normalized = _normalized_file_map(files)
    lines = [f"{sha256_hex(normalized[path])}  {path}\n" for path in sorted(normalized, key=lambda p: p.encode("utf-8"))]
    # The digest line uses ASCII separators/hex but the normalized path may
    # contain Unicode.  §13.4 specifies UTF-8 bytes for the complete manifest,
    # not an ASCII-only path profile.
    result = "".join(lines).encode("utf-8")
    if result.startswith(b"\xef\xbb\xbf") or not result.endswith(b"\n"):
        raise ContractError(ErrorCode.ASSET_ERROR, "files.sha256 must be UTF-8 LF with a final newline")
    return result


@dataclass(frozen=True)
class PackageDigest:
    files_sha256: bytes
    package_hash: str
    release_id: str


def _release_id(domain: str, identity: str, version: str, package_digest_value: str) -> str:
    return sha256_hex(f"{domain}\n{identity}\n{version}\n{package_digest_value}\n".encode("ascii"))


def package_hash(files: Mapping[str, bytes]) -> str:
    return sha256_hex(b"plotpilot-package/v1\n" + build_files_sha256(files))


def release_id(plugin_id: str, version: str, digest: str) -> str:
    if not _HEX64.fullmatch(digest):
        raise ContractError(ErrorCode.ASSET_ERROR, "package hash must be lowercase SHA-256")
    return _release_id("plotpilot-release/v1", plugin_id, version, digest)


def digest_package(files: Mapping[str, bytes], plugin_id: str, version: str) -> PackageDigest:
    files_sha256 = build_files_sha256(files)
    digest = sha256_hex(b"plotpilot-package/v1\n" + files_sha256)
    return PackageDigest(files_sha256, digest, release_id(plugin_id, version, digest))


def skill_package_hash(files: Mapping[str, bytes]) -> str:
    return sha256_hex(b"plotpilot-skill-package/v1\n" + build_files_sha256(files))


def skill_release_id(skill_id: str, version: str, digest: str) -> str:
    if not _HEX64.fullmatch(digest):
        raise ContractError(ErrorCode.ASSET_ERROR, "Skill package hash must be lowercase SHA-256")
    return _release_id("plotpilot-skill-release/v1", skill_id, version, digest)
