"""Strict TXT and paste decoding.

This module intentionally never uses a replacement codec.  Text is preserved
exactly, including CRLF, emoji, combining marks, and a final newline.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import zipfile
from typing import Any

from ..limits import DEFAULT_LIMITS, ImportLimits, ensure_size


class TxtDecodeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DecodeResult:
    text: str
    encoding: str
    had_bom: bool
    replaced: bool = False
    replacement_count: int = 0
    selection_source: str = "default"
    policy: str = "txt-lossless"
    policy_version: str = "v1"

    @property
    def lossless(self) -> bool:
        return not self.replaced and self.replacement_count == 0


_ALLOWED_ENCODINGS = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf16": "utf-16",
    "utf-16": "utf-16",
    "utf-16-le": "utf-16-le",
    "utf-16-be": "utf-16-be",
    "utf32": "utf-32",
    "utf-32": "utf-32",
    "utf-32-le": "utf-32-le",
    "utf-32-be": "utf-32-be",
    "gb18030": "gb18030",
    "gbk": "gbk",
    "gb2312": "gb2312",
}


def _looks_like_zip(raw: bytes) -> bool:
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return True
    try:
        return zipfile.is_zipfile(io.BytesIO(raw))
    except (OSError, ValueError):
        return False


def _has_binary_controls(text: str) -> bool:
    return any(
        (ord(character) < 32 and character not in "\t\n\r\f")
        or ord(character) == 0x7F
        for character in text
    )


def _encoding_from_bom(raw: bytes) -> tuple[str, bool] | None:
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
        return ("utf-32", True)
    if raw.startswith(b"\xef\xbb\xbf"):
        return ("utf-8-sig", True)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return ("utf-16", True)
    return None


def _decode_strict(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise TxtDecodeError("INVALID_ENCODING", f"strict TXT decode failed ({encoding})") from exc


def decode_txt(
    raw: bytes,
    *,
    source_kind: str = "txt",
    limits: ImportLimits = DEFAULT_LIMITS,
    encoding: str | None = None,
) -> DecodeResult:
    """Decode one Core-materialized TXT or paste Asset without lossy fallback."""
    if source_kind not in {"txt", "paste"}:
        raise TxtDecodeError("UNSUPPORTED_SOURCE_KIND", "TXT adapter accepts only txt or paste")
    if not isinstance(raw, bytes):
        raise TypeError("TXT input must be immutable bytes")
    ensure_size("original Asset", len(raw), limits.max_original_bytes)
    if _looks_like_zip(raw):
        raise TxtDecodeError("ZIP_MASQUERADE", "ZIP/EPUB bytes cannot be parsed as TXT")

    bom = _encoding_from_bom(raw)
    had_bom = bom is not None
    if encoding is not None:
        if not isinstance(encoding, str) or encoding.casefold() not in _ALLOWED_ENCODINGS:
            raise TxtDecodeError("UNSUPPORTED_ENCODING", "TXT encoding is outside the strict allowlist")
        selected = _ALLOWED_ENCODINGS[encoding.casefold()]
        if source_kind == "paste" and selected not in {"utf-8", "utf-8-sig"}:
            raise TxtDecodeError("PASTE_ENCODING", "paste Assets are UTF-8 only")
        selection_source = "explicit"
    elif bom is not None:
        selected, _ = bom
        selection_source = "bom"
    elif source_kind == "paste":
        selected = "utf-8"
        selection_source = "paste-utf8"
    else:
        selected = "utf-8"
        selection_source = "utf8-then-gb18030"

    if source_kind == "txt" and bom is None and encoding is None:
        try:
            text = raw.decode("utf-8", errors="strict")
            selected = "utf-8"
        except UnicodeDecodeError:
            selected = "gb18030"
            text = _decode_strict(raw, selected)
    else:
        text = _decode_strict(raw, selected)

    if "\ufffd" in text:
        raise TxtDecodeError("REPLACEMENT_CHARACTER", "TXT decode produced U+FFFD")
    if _has_binary_controls(text):
        raise TxtDecodeError("BINARY_CONTENT", "TXT contains binary control characters")
    ensure_size("decoded text", len(text), limits.max_text_characters)
    return DecodeResult(
        text=text,
        encoding="utf-8" if selected == "utf-8-sig" else selected,
        had_bom=had_bom,
        replaced=False,
        replacement_count=0,
        selection_source=selection_source,
    )


def txt_to_text(raw: bytes, **kwargs: Any) -> str:
    return decode_txt(raw, **kwargs).text


class TxtAdapter:
    """Small donor-compatible adapter facade with strict lossless semantics."""

    name = "txt"
    version = "1.0.0"
    extensions = (".txt",)
    formats = ("txt", "plain_text")

    def sniff(self, raw: bytes, file_name: str = "untitled.txt") -> float:
        try:
            decoded = decode_txt(raw, source_kind="txt")
        except (TypeError, ValueError):
            return 0.0
        if not decoded.text:
            return 0.4 if file_name.casefold().endswith(".txt") else 0.1
        score = sum(1 for char in decoded.text if char.isprintable() or char in "\r\n\t") / len(decoded.text)
        return min(1.0, score + (0.1 if file_name.casefold().endswith(".txt") else 0.0))

    def parse(self, raw: bytes, *, source_kind: str = "txt", **kwargs: Any) -> DecodeResult:
        return decode_txt(raw, source_kind=source_kind, **kwargs)
