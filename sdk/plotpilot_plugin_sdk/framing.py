"""Strict Content-Length framed JSON transport for Plugin RPC."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_bytes, parse_json_bytes
from .errors import ContractError, ErrorCode


HEADER_LIMIT = 8 * 1024
BODY_LIMIT = 8 * 1024 * 1024
_HEADER_RE = re.compile(
    rb"^Content-Length: ([0-9]+)\r\nContent-Type: application/json; charset=utf-8\r\n\r\n$"
)


def encode_frame(message: dict[str, Any]) -> bytes:
    # Canonical payloads make ACK-loss comparisons byte-stable across the
    # Python and TypeScript SDKs while retaining the exact stdio frame.
    try:
        payload = canonical_bytes(message)
    except Exception as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"RPC payload is not canonical JSON: {exc}") from exc
    if len(payload) > BODY_LIMIT:
        raise ContractError(ErrorCode.ASSET_ERROR, "RPC body exceeds 8 MiB")
    header = f"Content-Length: {len(payload)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode("ascii")
    if len(header) > HEADER_LIMIT:
        raise ContractError(ErrorCode.ASSET_ERROR, "RPC header exceeds 8 KiB")
    return header + payload


def decode_frame(frame: bytes) -> dict[str, Any]:
    marker = b"\r\n\r\n"
    separator = frame.find(marker)
    if separator < 0 or separator + len(marker) > HEADER_LIMIT:
        raise ContractError(ErrorCode.ASSET_ERROR, "invalid or oversized RPC header")
    header = frame[: separator + len(marker)]
    match = _HEADER_RE.fullmatch(header)
    if not match:
        raise ContractError(ErrorCode.ASSET_ERROR, "RPC header must use the exact Content-Length profile")
    length = int(match.group(1))
    if length > BODY_LIMIT:
        raise ContractError(ErrorCode.ASSET_ERROR, "RPC body exceeds 8 MiB")
    payload = frame[separator + len(marker) :]
    if len(payload) != length:
        raise ContractError(ErrorCode.ASSET_ERROR, "Content-Length does not match body bytes")
    value = parse_json_bytes(payload)
    if not isinstance(value, dict):
        raise ContractError(ErrorCode.ASSET_ERROR, "RPC batch and scalar payloads are forbidden")
    return value


@dataclass
class FrameDecoder:
    """Incremental decoder used by workers; it never consumes trailing bytes."""

    buffer: bytearray

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.buffer.extend(data)
        messages: list[dict[str, Any]] = []
        while True:
            marker = self.buffer.find(b"\r\n\r\n")
            if marker < 0:
                if len(self.buffer) > HEADER_LIMIT:
                    raise ContractError(ErrorCode.ASSET_ERROR, "RPC header exceeds 8 KiB")
                break
            header_end = marker + 4
            if header_end > HEADER_LIMIT:
                raise ContractError(ErrorCode.ASSET_ERROR, "RPC header exceeds 8 KiB")
            match = _HEADER_RE.fullmatch(bytes(self.buffer[:header_end]))
            if not match:
                raise ContractError(ErrorCode.ASSET_ERROR, "invalid RPC header")
            length = int(match.group(1))
            if length > BODY_LIMIT:
                raise ContractError(ErrorCode.ASSET_ERROR, "RPC body exceeds 8 MiB")
            total = header_end + length
            if len(self.buffer) < total:
                break
            payload = bytes(self.buffer[header_end:total])
            del self.buffer[:total]
            value = parse_json_bytes(payload)
            if not isinstance(value, dict):
                raise ContractError(ErrorCode.ASSET_ERROR, "RPC batch and scalar payloads are forbidden")
            messages.append(value)
        return messages
