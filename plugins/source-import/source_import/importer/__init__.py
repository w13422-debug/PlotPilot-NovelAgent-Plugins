"""Strict EPUB importer facade."""
from .epub import (
    EPUB_DECODER_POLICY,
    EPUB_DECODER_RECEIPT_SCHEMA,
    EPUB_DECODER_VERSION,
    EpubError,
    EpubParseResult,
    decode_epub,
    decode_epub_with_evidence,
    epub_to_text,
    looks_like_epub,
    parse_epub,
)

__all__ = [
    "EPUB_DECODER_POLICY",
    "EPUB_DECODER_RECEIPT_SCHEMA",
    "EPUB_DECODER_VERSION",
    "EpubError",
    "EpubParseResult",
    "decode_epub",
    "decode_epub_with_evidence",
    "epub_to_text",
    "looks_like_epub",
    "parse_epub",
]
