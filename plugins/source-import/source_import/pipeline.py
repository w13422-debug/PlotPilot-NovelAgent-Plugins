"""Core-owned Asset to deterministic source import pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable

from .adapters.txt import DecodeResult as TxtDecodeResult, decode_txt
from .contract import (
    build_canonical_receipt,
    build_decoder_receipt,
    build_provisional_receipt,
    build_raw_receipt,
    hash_jcs,
    sha256_hex,
)
from .importer.epub import EpubParseResult, parse_epub
from .limits import DEFAULT_LIMITS, ImportLimits
from .structure import StructureCancelled, build_structure_evidence


class SourceImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DecoderResult:
    text: str
    encoding: str
    had_bom: bool
    evidence: str = "default"
    replacement_count: int = 0

    @property
    def replaced(self) -> bool:
        return self.replacement_count != 0

    @property
    def lossless(self) -> bool:
        return self.replacement_count == 0


@dataclass(frozen=True)
class ParsedSource:
    raw_bytes: bytes
    source_asset_id: str | None
    source_asset_hash: str
    source_kind: str
    source_name: str | None
    decoded_text: str
    provisional_text: str
    canonical_text: str
    decoder: DecoderResult
    legacy_decoder_receipt: dict[str, Any]
    raw_receipt: dict[str, Any]
    decoder_receipt: dict[str, Any]
    provisional_receipt: dict[str, Any]
    canonical_receipt: dict[str, Any]
    structure_evidence: dict[str, Any]
    epub: EpubParseResult | None = None

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_text.encode("utf-8")

    @property
    def structure_evidence_hash(self) -> str:
        return str(self.structure_evidence["structure_hash"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_asset_id": self.source_asset_id,
            "source_asset_hash": self.source_asset_hash,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "text_hash": sha256_hex(self.canonical_bytes),
            "text_scalar_length": len(self.canonical_text),
            "encoding": self.decoder.encoding,
            "decoder_evidence": self.decoder.evidence,
            "receipts": {
                "raw": self.raw_receipt,
                "decoder": self.decoder_receipt,
                "provisional": self.provisional_receipt,
                "canonical": self.canonical_receipt,
                "legacy_decoder": self.legacy_decoder_receipt,
            },
            "structure_evidence": self.structure_evidence,
        }


def _legacy_decoder_receipt(parsed: ParsedSource | None, decoder: DecoderResult, source_kind: str) -> dict[str, Any]:
    policy = "epub-xml-lossless" if source_kind == "epub" else "paste-utf8-materialize" if source_kind == "paste" else "txt-lossless"
    encoding = decoder.evidence if source_kind == "epub" else decoder.encoding
    return {
        "schema": "source-cleaning-decoder-receipt/v1",
        "policy": policy,
        "policy_version": "v1",
        "encoding": encoding,
        "had_bom": decoder.had_bom,
        "replacement_count": decoder.replacement_count,
        "lossless": decoder.lossless,
    }


def parse_source(
    raw: bytes,
    *,
    source_kind: str,
    source_asset_id: str | None = None,
    source_asset_hash: str | None = None,
    source_name: str | None = None,
    limits: ImportLimits = DEFAULT_LIMITS,
    encoding: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
    provisional_asset_id: str | None = None,
    canonical_asset_id: str | None = None,
) -> ParsedSource:
    if source_kind not in {"txt", "epub", "paste"}:
        raise SourceImportError("UNSUPPORTED_SOURCE_KIND", "only TXT, EPUB and paste are supported")
    if not isinstance(raw, bytes):
        raise TypeError("source input must be immutable bytes")
    actual_hash = sha256_hex(raw)
    if source_asset_hash is not None and actual_hash != source_asset_hash:
        raise SourceImportError("ASSET_HASH_MISMATCH", "Core Asset bytes do not match source_asset_hash")
    bound_hash = source_asset_hash or actual_hash
    raw_receipt = build_raw_receipt(
        source_asset_id=source_asset_id,
        source_asset_hash=bound_hash,
        source_kind=source_kind,
        source_name=source_name,
        size_bytes=len(raw),
    )
    if cancel_check is not None and cancel_check():
        raise SourceImportError("CANCELLED", "source import cancelled")
    epub_result: EpubParseResult | None = None
    if source_kind == "epub":
        try:
            epub_result = parse_epub(raw, limits=limits, cancel_check=cancel_check)
        except Exception as exc:
            if getattr(exc, "code", None) == "CANCELLED":
                raise
            if exc.__class__.__name__ == "_Cancelled":
                raise SourceImportError("CANCELLED", "source import cancelled") from exc
            raise
        decoder = DecoderResult(epub_result.text, "epub", epub_result.had_bom, epub_result.encoding_evidence)
    else:
        try:
            decoded = decode_txt(raw, source_kind=source_kind, limits=limits, encoding=encoding)
        except Exception as exc:
            if getattr(exc, "code", None) == "CANCELLED":
                raise
            raise
        decoder = DecoderResult(
            decoded.text,
            decoded.encoding,
            decoded.had_bom,
            decoded.selection_source,
            decoded.replacement_count,
        )
    if cancel_check is not None and cancel_check():
        raise SourceImportError("CANCELLED", "source import cancelled")
    decoded_text = decoder.text
    provisional_text = decoded_text
    canonical_text = provisional_text
    text_hash = sha256_hex(canonical_text.encode("utf-8"))
    try:
        structure_evidence = build_structure_evidence(
            canonical_text,
            source_asset_id=source_asset_id,
            source_asset_hash=bound_hash,
            limits=limits,
            cancel_check=cancel_check,
        )
    except StructureCancelled as exc:
        raise SourceImportError("CANCELLED", "source import cancelled") from exc
    decoder_receipt = build_decoder_receipt(
        raw_receipt=raw_receipt,
        encoding=decoder.encoding,
        selection_source=decoder.evidence,
        had_bom=decoder.had_bom,
        replacement_count=decoder.replacement_count,
        decoded_text_hash=text_hash,
    )
    provisional_receipt = build_provisional_receipt(
        raw_receipt=raw_receipt,
        decoder_receipt=decoder_receipt,
        provisional_asset_id=provisional_asset_id,
        provisional_text_hash=text_hash,
        provisional_byte_length=len(provisional_text.encode("utf-8")),
        structure_evidence_hash=structure_evidence["structure_hash"],
        source_kind=source_kind,
    )
    canonical_receipt = build_canonical_receipt(
        raw_receipt=raw_receipt,
        decoder_receipt=decoder_receipt,
        provisional_receipt=provisional_receipt,
        canonical_asset_id=canonical_asset_id,
        canonical_text_hash=text_hash,
        canonical_byte_length=len(canonical_text.encode("utf-8")),
        structure_evidence_hash=structure_evidence["structure_hash"],
    )
    return ParsedSource(
        raw_bytes=raw,
        source_asset_id=source_asset_id,
        source_asset_hash=bound_hash,
        source_kind=source_kind,
        source_name=source_name,
        decoded_text=decoded_text,
        provisional_text=provisional_text,
        canonical_text=canonical_text,
        decoder=decoder,
        legacy_decoder_receipt=_legacy_decoder_receipt(None, decoder, source_kind),
        raw_receipt=raw_receipt,
        decoder_receipt=decoder_receipt,
        provisional_receipt=provisional_receipt,
        canonical_receipt=canonical_receipt,
        structure_evidence=structure_evidence,
        epub=epub_result,
    )


def import_source(*args: Any, **kwargs: Any) -> ParsedSource:
    return parse_source(*args, **kwargs)
