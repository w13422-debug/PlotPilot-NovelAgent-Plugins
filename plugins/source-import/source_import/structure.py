"""Deterministic Unicode-scalar structure evidence for imported text."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Callable

from .limits import DEFAULT_LIMITS, ImportLimits, ensure_count

COORDINATE_SYSTEM = "python-unicode-codepoint/v1"
EVIDENCE_SCHEMA = "source-structure-evidence/v1"
_NODE_SCHEMA = "source-structure-node/v1"
_SPAN_SCHEMA = "source-structure-span/v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMBER = r"[0-9０-９零〇一二两三四五六七八九十百千万]+"
_VOLUME = re.compile(rf"^\s*第(?P<number>{_NUMBER})卷(?:[ \t　]+(?P<title>.*))?$")
_CHAPTER = re.compile(rf"^\s*第(?P<number>{_NUMBER})[章回节话](?P<title>.*)$")


class StructureError(ValueError):
    pass


class StructureCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class StructureResult:
    evidence: dict[str, Any]
    nodes: tuple[dict[str, Any], ...]
    spans: tuple[dict[str, Any], ...]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_jcs(prefix: str, value: Any) -> str:
    try:
        from plotpilot_plugin_sdk.canonical import hash_jcs
    except ImportError as exc:
        raise StructureError("public PlotPilot SDK is unavailable") from exc
    return str(hash_jcs(prefix, value))


def _number(value: str) -> str:
    return value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise StructureCancelled("source structure parsing cancelled")


def _line_records(text: str, cancel_check: Callable[[], bool] | None = None) -> list[tuple[int, int, str]]:
    records: list[tuple[int, int, str]] = []
    start = 0
    _check_cancel(cancel_check)
    for line in text.splitlines(keepends=True):
        _check_cancel(cancel_check)
        end = start + len(line)
        content = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith(("\n", "\r")) else line
        records.append((start, end, content))
        start = end
    if not records or start < len(text):
        records.append((start, len(text), text[start:]))
    return records


def _node_id(kind: str, start: int, end: int, title: str) -> str:
    digest = _sha256(f"{kind}\n{start}\n{end}\n{title}".encode("utf-8"))
    return f"source-node-{digest[:32]}"


def _span_id(node_id: str, start: int, end: int) -> str:
    return f"source-span-{_sha256(f'{node_id}\n{start}\n{end}'.encode('ascii'))[:32]}"


def _canonical_hash(text: str) -> str:
    return _sha256(text.encode("utf-8"))


def _make_span(
    *,
    node_id: str,
    kind: str,
    start: int,
    end: int,
    quote: str,
    canonical_text_hash: str,
    source_asset_id: str | None,
    source_asset_hash: str | None,
) -> dict[str, Any]:
    return {
        "schema": _SPAN_SCHEMA,
        "span_id": _span_id(node_id, start, end),
        "node_id": node_id,
        "kind": kind,
        "source_asset_id": source_asset_id,
        "source_asset_hash": source_asset_hash,
        "coordinate_system": COORDINATE_SYSTEM,
        "start_codepoint": start,
        "end_codepoint": end,
        "quote": quote,
        "quote_hash": _sha256(quote.encode("utf-8")),
        "canonical_text_hash": canonical_text_hash,
    }


def build_structure_evidence(
    text: str,
    *,
    source_asset_id: str | None = None,
    source_asset_hash: str | None = None,
    limits: ImportLimits = DEFAULT_LIMITS,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Return ordered, hash-bound structure spans without inventing Core IDs."""
    if not isinstance(text, str):
        raise TypeError("structure input must be text")
    ensure_count("structure scalar count", len(text), limits.max_text_characters)
    _check_cancel(cancel_check)
    text_hash = _canonical_hash(text)
    records = _line_records(text, cancel_check)
    heading_records: list[tuple[int, int, str, str]] = []
    for start, end_with_newline, content in records:
        _check_cancel(cancel_check)
        end = start + len(content)
        if not content.strip():
            continue
        volume = _VOLUME.match(content)
        chapter = _CHAPTER.match(content)
        if volume and not content.startswith(("　", " ", "\t")):
            title = (volume.group("title") or "").strip()
            heading_records.append((start, end, "volume", title))
        elif chapter and not content.startswith(("　", " ", "\t")):
            title = chapter.group("title").strip().lstrip("　 \t:：-—")
            heading_records.append((start, end, "chapter", title))
    ordered_nodes: list[dict[str, Any]] = []
    ordered_spans: list[dict[str, Any]] = []
    volume_node_id: str | None = None
    chapter_node_id: str | None = None
    node_ordinal = 0
    for start, end, content in records:
        _check_cancel(cancel_check)
        if not content.strip():
            continue
        heading_kind = None
        heading_title = ""
        for h_start, h_end, h_kind, h_title in heading_records:
            if start == h_start and end == h_end:
                heading_kind, heading_title = h_kind, h_title
                break
        if heading_kind == "volume":
            volume_node_id = _node_id("volume", start, end, heading_title)
            chapter_node_id = None
            node = {
                "schema": _NODE_SCHEMA,
                "node_id": volume_node_id,
                "ordinal": node_ordinal,
                "kind": "volume",
                "title": heading_title,
                "depth": 0,
                "parent_node_id": None,
                "start_codepoint": start,
                "end_codepoint": end,
                "span_ids": [],
            }
            node_ordinal += 1
        elif heading_kind == "chapter":
            chapter_node_id = _node_id("chapter", start, end, heading_title)
            node = {
                "schema": _NODE_SCHEMA,
                "node_id": chapter_node_id,
                "ordinal": node_ordinal,
                "kind": "chapter",
                "title": heading_title,
                "depth": 1 if volume_node_id else 0,
                "parent_node_id": volume_node_id,
                "start_codepoint": start,
                "end_codepoint": end,
                "span_ids": [],
            }
            node_ordinal += 1
        else:
            parent = chapter_node_id or volume_node_id
            node_id = _node_id("paragraph", start, end, content)
            node = {
                "schema": _NODE_SCHEMA,
                "node_id": node_id,
                "ordinal": node_ordinal,
                "kind": "paragraph",
                "title": "",
                "depth": 2 if chapter_node_id and volume_node_id else 1 if parent else 0,
                "parent_node_id": parent,
                "start_codepoint": start,
                "end_codepoint": end,
                "span_ids": [],
            }
            node_ordinal += 1
        span = _make_span(
            node_id=node["node_id"],
            kind=node["kind"],
            start=start,
            end=end,
            quote=text[start:end],
            canonical_text_hash=text_hash,
            source_asset_id=source_asset_id,
            source_asset_hash=source_asset_hash,
        )
        node["span_ids"].append(span["span_id"])
        ordered_nodes.append(node)
        ordered_spans.append(span)
    ensure_count("structure nodes", len(ordered_nodes), limits.max_structure_nodes)
    _check_cancel(cancel_check)
    body: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "coordinate_system": COORDINATE_SYSTEM,
        "source_asset_id": source_asset_id,
        "source_asset_hash": source_asset_hash,
        "canonical_text_hash": text_hash,
        "scalar_length": len(text),
        "nodes": ordered_nodes,
        "ordered_spans": ordered_spans,
        "heading_count": len(heading_records),
    }
    body["structure_hash"] = _hash_jcs(EVIDENCE_SCHEMA, body)
    return body


def structure_result(text: str, **kwargs: Any) -> StructureResult:
    evidence = build_structure_evidence(text, **kwargs)
    return StructureResult(evidence, tuple(evidence["nodes"]), tuple(evidence["ordered_spans"]))
