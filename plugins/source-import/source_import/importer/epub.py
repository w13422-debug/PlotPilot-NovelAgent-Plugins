"""Bounded, lossless EPUB 2/3 decoder.

Only standard-library XML/ZIP primitives are used.  Archive entries are
validated before reads, XML is decoded strictly, and the original EPUB bytes
are never rewritten or repacked.
"""
from __future__ import annotations

from dataclasses import dataclass
import codecs
import html
import io
import posixpath
import re
import stat
from typing import Callable, Iterable
from urllib.parse import unquote
import zipfile
from xml.etree import ElementTree as ET

from ..limits import DEFAULT_LIMITS, ImportLimits, ensure_count, ensure_size

EPUB_DECODER_POLICY = "epub-xml-lossless"
EPUB_DECODER_VERSION = "v1"
EPUB_DECODER_RECEIPT_SCHEMA = "source-cleaning-decoder-receipt/v1"
_REPLACEMENT = "\ufffd"
_XML_DECLARATION = re.compile(r"\A\s*<\?xml\b(?P<body>.*?)\?>", re.IGNORECASE | re.DOTALL)
_ENCODING_ATTRIBUTE = re.compile(r"""\bencoding\s*=\s*(['"])(?P<encoding>[^'"]+)\1""", re.IGNORECASE)
_META_CHARSET = re.compile(r"""<meta\b[^>]*?\bcharset\s*=\s*(['"]?)(?P<encoding>[A-Za-z0-9._:-]+)\1""", re.IGNORECASE)
_CHAPTER_PREFIX = re.compile(r"^\s*第[0-9０-９零〇一二两三四五六七八九十百千万]+[章回节话](?P<rest>.*)$")
_VOLUME_RE = re.compile(r"^\s*(?:第[0-9０-９零〇一二两三四五六七八九十百千万]+[卷部]|卷[0-9０-９零〇-九一二两三四五六七八九十百千万]*|[0-9０-９]+[部卷]).*$")
_BLOCKS = {"p", "div", "section", "article", "blockquote", "pre", "li", "h1", "h2", "h3", "h4", "h5", "h6", "title", "dt", "dd"}
_SKIP = {"script", "style", "noscript", "template", "nav"}
_ALLOWED_CODECS = {
    "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
    "utf-32", "utf-32-le", "utf-32-be", "gb18030", "gbk", "gb2312",
    "iso-8859-1",
}


class EpubError(ValueError):
    def __init__(self, code: str, message: str, *, member: str | None = None) -> None:
        self.code = code
        self.member = member
        prefix = f"EPUB strict decode failed for {member!r}: " if member else "EPUB import failed: "
        super().__init__(prefix + message)


class _Cancelled(Exception):
    pass


@dataclass(frozen=True)
class EpubParseResult:
    text: str
    had_bom: bool
    encoding_evidence: str
    source_members: tuple[str, ...]
    chapters: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _DecodedMarkup:
    text: str
    had_bom: bool
    codec: str
    selection_source: str

    @property
    def evidence(self) -> str:
        return f"{self.codec}@{self.selection_source}"


@dataclass(frozen=True)
class _TextDocument:
    text: str
    anchors: dict[str, int]
    heading_offsets: tuple[int, ...]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold() if isinstance(tag, str) else ""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise _Cancelled()


def _safe_archive_name(name: str, *, directory: bool = False) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise EpubError("PATH_TRAVERSAL", "archive member path is not a safe POSIX path", member=name)
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise EpubError("PATH_TRAVERSAL", "absolute archive member path is forbidden", member=name)
    if directory:
        if not name.endswith("/"):
            raise EpubError("PATH_TRAVERSAL", "directory member must end with slash", member=name)
        canonical = name[:-1]
    else:
        canonical = name
    parts = canonical.split("/")
    if not canonical or any(part in {"", ".", ".."} for part in parts):
        raise EpubError("PATH_TRAVERSAL", "archive member contains traversal", member=name)
    if len(name) > 240:
        raise EpubError("PATH_TRAVERSAL", "archive member path is too long", member=name)
    return canonical


def _safe_href(source: str, target: str) -> str:
    if not isinstance(target, str):
        raise EpubError("PATH_TRAVERSAL", "EPUB link target is not text")
    value = unquote(target.strip())
    if not value or "\x00" in value or "\\" in value:
        raise EpubError("PATH_TRAVERSAL", "EPUB link target is unsafe")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or value.startswith("//") or value.startswith("/"):
        raise EpubError("PATH_TRAVERSAL", "absolute or scheme link is forbidden")
    path, marker, fragment = value.partition("#")
    if "?" in path:
        raise EpubError("PATH_TRAVERSAL", "query links are not accepted")
    base = posixpath.dirname(source)
    candidate = posixpath.normpath(posixpath.join(base, path)) if path else source
    if candidate in {"", ".", ".."} or candidate.startswith("../") or "/../" in candidate:
        raise EpubError("PATH_TRAVERSAL", "EPUB link escapes archive root")
    _safe_archive_name(candidate)
    if marker:
        if not fragment or any(ord(c) < 32 for c in fragment):
            raise EpubError("PATH_TRAVERSAL", "EPUB fragment is unsafe")
        return candidate + "#" + fragment
    return candidate


def _codec_name(label: str, member: str) -> str:
    normalized = label.strip().casefold().replace("_", "-")
    if normalized not in _ALLOWED_CODECS:
        raise EpubError("UNSUPPORTED_ENCODING", f"unknown or non-text encoding {label!r}", member=member)
    try:
        info = codecs.lookup(normalized)
    except LookupError as exc:
        raise EpubError("UNSUPPORTED_ENCODING", f"unknown encoding {label!r}", member=member) from exc
    return info.name


def _bom_codec(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return None


def _signature_codec(raw: bytes) -> str | None:
    if raw.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if raw.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    if raw.startswith(b"\x00<"):
        return "utf-16-be"
    if raw.startswith(b"<\x00"):
        return "utf-16-le"
    return None


def _declaration(probe: str) -> tuple[bool, str | None]:
    match = _XML_DECLARATION.match(probe)
    if match is None:
        return False, None
    encoding = _ENCODING_ATTRIBUTE.search(match.group("body"))
    return True, encoding.group("encoding").strip() if encoding else None


def _html_meta(probe: str) -> str | None:
    scrubbed = re.sub(r"<!--.*?-->", "", probe, flags=re.DOTALL)
    match = _META_CHARSET.search(scrubbed)
    return match.group("encoding").strip() if match else None


def _codec_family(codec: str, raw: bytes) -> tuple[str, str | None]:
    name = codecs.lookup(codec).name.replace("_", "-").casefold()
    if name in {"utf-8", "utf-8-sig"}:
        return "utf-8", None
    if name in {"utf-16", "utf-16-le", "utf-16-be"}:
        order = "le" if raw.startswith(b"\xff\xfe") or name.endswith("-le") else "be" if raw.startswith(b"\xfe\xff") or name.endswith("-be") else None
        return "utf-16", order
    if name in {"utf-32", "utf-32-le", "utf-32-be"}:
        order = "le" if raw.startswith(b"\xff\xfe\x00\x00") or name.endswith("-le") else "be" if raw.startswith(b"\x00\x00\xfe\xff") or name.endswith("-be") else None
        return "utf-32", order
    return name, None


def _compatible(selected: str, declared: str, raw: bytes) -> bool:
    sf, so = _codec_family(selected, raw)
    df, do = _codec_family(declared, raw)
    if sf != df:
        return False
    return sf not in {"utf-16", "utf-32"} or do is None or so == do


def _strict_decode(raw: bytes, member: str, *, allow_html_meta: bool, limits: ImportLimits) -> _DecodedMarkup:
    ensure_size("EPUB XML member", len(raw), limits.max_xml_bytes)
    bom = _bom_codec(raw)
    signature = None if bom else _signature_codec(raw)
    probe_codec = bom or signature
    try:
        probe = raw.decode(probe_codec or "latin-1", errors="strict")
    except UnicodeDecodeError as exc:
        raise EpubError("INVALID_ENCODING", "invalid markup bytes", member=member) from exc
    has_decl, declared = _declaration(probe)
    if has_decl and declared:
        declared_codec = _codec_name(declared, member)
        selected = bom or signature or declared_codec
        source = "bom" if bom else "signature" if signature else "xml"
        if not _compatible(selected, declared_codec, raw):
            raise EpubError("ENCODING_MISMATCH", f"encoding declaration mismatch ({declared!r})", member=member)
    elif has_decl:
        selected, source = bom or signature or "utf-8", "xml"
    elif bom:
        selected, source = bom, "bom"
    elif signature:
        selected, source = signature, "signature"
    elif allow_html_meta:
        meta = _html_meta(probe)
        selected, source = (_codec_name(meta, member), "html-meta") if meta else ("utf-8", "default")
    else:
        selected, source = "utf-8", "default"
    try:
        text = raw.decode(selected, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise EpubError("INVALID_ENCODING", f"strict markup decode failed ({selected})", member=member) from exc
    if _REPLACEMENT in text or _REPLACEMENT in html.unescape(text):
        raise EpubError("REPLACEMENT_CHARACTER", "replacement character is forbidden", member=member)
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        raise EpubError("INVALID_XML", "XML contains forbidden control characters", member=member)
    return _DecodedMarkup(text, bom is not None, codecs.lookup(selected).name, source)


def _assert_tree(root: ET.Element, member: str, limits: ImportLimits) -> None:
    if "<!doctype" in ET.tostring(root, encoding="unicode").casefold():
        raise EpubError("DOCTYPE_FORBIDDEN", "DOCTYPE/ENTITY is forbidden", member=member)
    depth = 0
    for element in root.iter():
        depth = max(depth, 1)
        values = [element.tag, element.text, element.tail, *element.attrib.keys(), *element.attrib.values()]
        if any(value is not None and _REPLACEMENT in value for value in values):
            raise EpubError("REPLACEMENT_CHARACTER", "replacement character is forbidden", member=member)
    def walk(node: ET.Element, current: int) -> None:
        if current > limits.max_xml_depth:
            raise EpubError("XML_DEPTH", "XML nesting depth exceeds the resource limit", member=member)
        for child in list(node):
            walk(child, current + 1)
    walk(root, 1)


def _parse_xml(raw: bytes, member: str, limits: ImportLimits, *, xhtml: bool = False) -> tuple[ET.Element, _DecodedMarkup]:
    decoded = _strict_decode(raw, member, allow_html_meta=xhtml, limits=limits)
    if "<!doctype" in decoded.text.casefold() or "<!entity" in decoded.text.casefold():
        raise EpubError("DOCTYPE_FORBIDDEN", "DOCTYPE/ENTITY is forbidden", member=member)
    try:
        root = ET.fromstring(decoded.text)
    except ET.ParseError as exc:
        raise EpubError("MALFORMED_XML", "malformed XML/XHTML", member=member) from exc
    _assert_tree(root, member, limits)
    return root, decoded


def _archive_infos(raw: bytes, limits: ImportLimits) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    ensure_size("original Asset", len(raw), limits.max_original_bytes)
    if not raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise EpubError("NOT_EPUB", "EPUB is not a ZIP archive")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise EpubError("INVALID_ZIP", "invalid ZIP/EPUB archive") from exc
    infos = archive.infolist()
    ensure_count("zip entries", len(infos), limits.max_zip_entries)
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        is_directory = info.is_dir() or info.filename.endswith("/")
        name = _safe_archive_name(info.filename, directory=is_directory)
        folded = name.casefold()
        if folded in {key.casefold() for key in result}:
            archive.close()
            raise EpubError("DUPLICATE_MEMBER", "case-insensitive duplicate archive member", member=name)
        if info.flag_bits & 0x1:
            archive.close()
            raise EpubError("ENCRYPTED_MEMBER", "encrypted ZIP member is forbidden", member=name)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            archive.close()
            raise EpubError("SYMLINK_MEMBER", "symlink ZIP member is forbidden", member=name)
        ensure_size("zip member bytes", info.file_size, limits.max_zip_member_bytes)
        total += info.file_size
        ensure_size("zip total bytes", total, limits.max_zip_total_bytes)
        if info.file_size and info.compress_size <= 0:
            archive.close()
            raise EpubError("ZIP_RATIO", "zero-size compressed member has data", member=name)
        if info.file_size and info.compress_size and info.file_size / info.compress_size > limits.max_zip_compression_ratio:
            archive.close()
            raise EpubError("ZIP_RATIO", "ZIP compression ratio exceeds limit", member=name)
        if not is_directory:
            result[name] = info
    if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
        archive.close()
        raise EpubError("EPUB_MIMETYPE", "mimetype must be the first stored member")
    try:
        mime = archive.read(infos[0]).decode("ascii", errors="strict")
    except (UnicodeDecodeError, OSError, zipfile.BadZipFile) as exc:
        archive.close()
        raise EpubError("EPUB_MIMETYPE", "mimetype cannot be read strictly") from exc
    if mime != "application/epub+zip":
        archive.close()
        raise EpubError("EPUB_MIMETYPE", "mimetype payload is not application/epub+zip")
    if "META-INF/container.xml" not in result:
        archive.close()
        raise EpubError("EPUB_CONTAINER", "META-INF/container.xml is missing")
    return archive, result


def looks_like_epub(raw: bytes, *, limits: ImportLimits = DEFAULT_LIMITS) -> bool:
    try:
        archive, infos = _archive_infos(raw, limits)
    except (EpubError, TypeError, ValueError):
        return False
    archive.close()
    return "META-INF/container.xml" in infos


def _read(archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo], name: str) -> bytes:
    if name not in infos:
        raise EpubError("MISSING_MEMBER", f"required EPUB member is missing: {name}", member=name)
    try:
        return archive.read(infos[name])
    except (OSError, zipfile.BadZipFile) as exc:
        raise EpubError("INVALID_ZIP", f"cannot read EPUB member: {name}", member=name) from exc


def _text_content(element: ET.Element) -> str:
    chunks: list[str] = []
    for node in element.iter():
        if _local(node.tag) in _SKIP:
            continue
        if node.text:
            chunks.append(node.text)
        if node.tail:
            chunks.append(node.tail)
    return re.sub(r"[\t\r\n ]+", " ", "".join(chunks)).strip()


def _extract_xhtml(root: ET.Element, *, max_characters: int) -> _TextDocument:
    lines: list[str] = []
    text_length = 0
    anchors: dict[str, int] = {}
    headings: list[int] = []
    def ensure_line_budget(value: str) -> None:
        nonlocal text_length
        added = len(value) + (1 if lines else 0)
        ensure_size("decoded document text", text_length + added, max_characters)
        text_length += added
    def has_block_child(node: ET.Element) -> bool:
        return any(_local(child.tag) in _BLOCKS for child in list(node))
    def walk(node: ET.Element) -> None:
        tag = _local(node.tag)
        if tag in _SKIP:
            return
        if tag in _BLOCKS and not has_block_child(node):
            offset = sum(len(line) + 1 for line in lines)
            for attr in ("id", "name"):
                if node.get(attr):
                    anchors[str(node.get(attr))] = offset
            value = _text_content(node)
            if value:
                ensure_line_budget(value)
                lines.append(value)
                if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "title"}:
                    headings.append(offset)
            return
        for attr in ("id", "name"):
            if node.get(attr):
                anchors[str(node.get(attr))] = sum(len(line) + 1 for line in lines)
        for child in list(node):
            walk(child)
    walk(root)
    if lines:
        ensure_size("decoded document text", text_length + 1, max_characters)
    text = "\n".join(lines) + ("\n" if lines else "")
    return _TextDocument(text, anchors, tuple(headings))


def _label(root: ET.Element) -> str:
    return _text_content(root)


def _item_local(root: ET.Element, name: str) -> Iterable[ET.Element]:
    return (element for element in root.iter() if _local(element.tag) == name)


def _parse_nav_entries(root: ET.Element, *, nav_type: str = "ncx") -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if nav_type == "ncx":
        for point in _item_local(root, "navpoint"):
            title = next((_label(node) for node in point.iter() if _local(node.tag) == "text"), "")
            content = next((node.get("src") for node in point.iter() if _local(node.tag) == "content"), None)
            if content:
                entries.append((title, str(content)))
    else:
        nav_nodes = [node for node in root.iter() if _local(node.tag) == "nav"]
        for nav in nav_nodes[:1]:
            for link in nav.iter():
                if _local(link.tag) != "a" or not link.get("href"):
                    continue
                entries.append((_label(link), str(link.get("href"))))
    return entries


def _is_volume(title: str) -> bool:
    return bool(_VOLUME_RE.match(title.strip())) and not _CHAPTER_PREFIX.match(title.strip())


def _clean_title(title: str) -> str:
    value = title.strip()
    match = _CHAPTER_PREFIX.match(value)
    if match:
        value = match.group("rest").strip()
    return value.strip(" \t　:：-—")


def _encoding_summary(evidence: set[str]) -> str:
    summary = ",".join(sorted(evidence))
    if not summary or len(summary) > 128:
        raise EpubError("ENCODING_EVIDENCE", "encoding evidence exceeds contract", member="<archive>")
    return summary


def parse_epub(raw: bytes, *, limits: ImportLimits = DEFAULT_LIMITS, cancel_check: Callable[[], bool] | None = None) -> EpubParseResult:
    _check_cancel(cancel_check)
    archive, infos = _archive_infos(raw, limits)
    try:
        container, container_decoded = _parse_xml(_read(archive, infos, "META-INF/container.xml"), "META-INF/container.xml", limits)
        evidence = {container_decoded.evidence}
        had_bom = container_decoded.had_bom
        rootfile = next((node for node in _item_local(container, "rootfile") if node.get("full-path")), None)
        if rootfile is None:
            raise EpubError("EPUB_CONTAINER", "container has no rootfile full-path")
        opf_name = _safe_archive_name(str(rootfile.get("full-path")))
        if str(rootfile.get("media-type") or "application/oebps-package+xml") != "application/oebps-package+xml":
            raise EpubError("EPUB_CONTAINER", "rootfile media-type is not OPF")
        opf, opf_decoded = _parse_xml(_read(archive, infos, opf_name), opf_name, limits)
        evidence.add(opf_decoded.evidence)
        had_bom = had_bom or opf_decoded.had_bom
        base = posixpath.dirname(opf_name)
        manifest: dict[str, dict[str, str]] = {}
        for item in _item_local(opf, "item"):
            item_id, href = item.get("id"), item.get("href")
            if not item_id or not href:
                continue
            if item_id in manifest:
                raise EpubError("EPUB_OPF", "manifest contains duplicate item IDs")
            full = _safe_href(base + "/__opf__" if base else "__opf__", str(href))
            # _safe_href above resolves relative to the OPF directory; remove sentinel.
            full = full.replace("/__opf__", "") if full.endswith("/__opf__") else full
            if base:
                full = _safe_archive_name(posixpath.normpath(posixpath.join(base, unquote(str(href)))))
            else:
                full = _safe_archive_name(posixpath.normpath(unquote(str(href))))
            manifest[str(item_id)] = {
                "href": full,
                "media_type": str(item.get("media-type") or ""),
                "properties": str(item.get("properties") or ""),
            }
        spine_itemrefs: list[str] = []
        ncx_id: str | None = None
        spine_node = next((node for node in _item_local(opf, "spine")), None)
        if spine_node is not None:
            ncx_id = spine_node.get("toc")
            for itemref in _item_local(spine_node, "itemref"):
                if itemref.get("idref"):
                    spine_itemrefs.append(str(itemref.get("idref")))
        ensure_count("spine items", len(spine_itemrefs), limits.max_spine_items)
        spine: list[str] = []
        for item_id in spine_itemrefs:
            if item_id not in manifest:
                raise EpubError("EPUB_SPINE", f"spine idref is not in manifest: {item_id}")
            spine.append(manifest[item_id]["href"])
        documents: dict[str, _TextDocument] = {}
        member_order: list[str] = []
        for full in spine:
            _check_cancel(cancel_check)
            if full in documents:
                raise EpubError("EPUB_SPINE", "spine contains duplicate content")
            markup, decoded = _parse_xml(_read(archive, infos, full), full, limits, xhtml=True)
            evidence.add(decoded.evidence)
            had_bom = had_bom or decoded.had_bom
            documents[full] = _extract_xhtml(markup, max_characters=limits.max_text_characters)
            member_order.append(full)
        global_text_parts: list[str] = []
        accumulated_text_length = 0
        global_base: dict[str, int] = {}
        global_anchor: dict[str, int] = {}
        for full in member_order:
            document = documents[full]
            global_base[full] = sum(len(part) for part in global_text_parts)
            for anchor, offset in document.anchors.items():
                global_anchor[full + "#" + anchor] = global_base[full] + offset
            global_anchor[full] = global_base[full]
            ensure_size(
                "decoded document text",
                accumulated_text_length + len(document.text),
                limits.max_text_characters,
            )
            accumulated_text_length += len(document.text)
            global_text_parts.append(document.text)
        full_text = "".join(global_text_parts)
        nav_entries: list[tuple[str, str]] = []
        ncx_item = manifest.get(ncx_id or "")
        if ncx_item is None:
            ncx_item = next((item for item in manifest.values() if item["media_type"] == "application/x-dtbncx+xml"), None)
        if ncx_item is not None:
            ncx_path = ncx_item["href"]
            ncx, decoded = _parse_xml(_read(archive, infos, ncx_path), ncx_path, limits)
            evidence.add(decoded.evidence)
            had_bom = had_bom or decoded.had_bom
            for title, src in _parse_nav_entries(ncx):
                try:
                    nav_entries.append((title, _safe_href(ncx_path, src)))
                except EpubError:
                    raise
        else:
            nav_item = next((item for item in manifest.values() if "nav" in item["properties"].split()), None)
            if nav_item is not None:
                nav_path = nav_item["href"]
                nav, decoded = _parse_xml(_read(archive, infos, nav_path), nav_path, limits, xhtml=True)
                evidence.add(decoded.evidence)
                had_bom = had_bom or decoded.had_bom
                for title, src in _parse_nav_entries(nav, nav_type="nav"):
                    nav_entries.append((title, _safe_href(nav_path, src)))
        ensure_count("navigation entries", len(nav_entries), limits.max_nav_entries)
        resolved: list[tuple[str, int, int, str]] = []
        for ordinal, (title, src) in enumerate(nav_entries):
            offset = global_anchor.get(src)
            if offset is None:
                offset = global_base.get(src.split("#", 1)[0])
            if offset is not None:
                resolved.append((title.strip(), offset, ordinal, src))
        resolved.sort(key=lambda item: (item[1], item[2]))
        if not resolved:
            for ordinal, full in enumerate(member_order):
                resolved.append((f"第{ordinal + 1}节", global_base[full], ordinal, full))
        output_lines: list[str] = []
        formatted_length = 0
        chapters: list[dict[str, object]] = []
        volume_number = chapter_number = 0
        def append_formatted(value: str) -> None:
            nonlocal formatted_length
            added = len(value) + (1 if output_lines else 0)
            ensure_size("formatted output", formatted_length + added, limits.max_text_characters)
            formatted_length += added
            output_lines.append(value)
        for index, (title, offset, _ordinal, src) in enumerate(resolved):
            end = resolved[index + 1][1] if index + 1 < len(resolved) else len(full_text)
            body = full_text[offset:end].strip("\n")
            if title in {"目录", "丛书目录", "开篇", "开 篇"} and len(body) < 80:
                continue
            if _is_volume(title):
                volume_number += 1
                chapter_number = 0
                heading = f"第{volume_number}卷 {title}"
                kind = "volume"
            else:
                if volume_number == 0:
                    volume_number = 1
                chapter_number += 1
                cleaned = _clean_title(title) or title[:24]
                heading = f"第{chapter_number}章　{cleaned}"
                kind = "chapter"
            indented = "\n".join(("　" + line.strip()) if line.strip() else "" for line in body.split("\n"))
            append_formatted(heading)
            append_formatted(indented)
            append_formatted("")
            chapters.append({"kind": kind, "title": title, "source": src, "start": offset, "end": end})
        text = "\n".join(output_lines)
        ensure_size("formatted output", len(text), limits.max_text_characters)
        return EpubParseResult(text, had_bom, _encoding_summary(evidence), tuple(member_order), tuple(chapters))
    except _Cancelled:
        raise
    finally:
        archive.close()


def epub_to_text(raw: bytes, *, limits: ImportLimits = DEFAULT_LIMITS, cancel_check: Callable[[], bool] | None = None) -> str:
    return parse_epub(raw, limits=limits, cancel_check=cancel_check).text


def decode_epub(raw: bytes, *, limits: ImportLimits = DEFAULT_LIMITS, cancel_check: Callable[[], bool] | None = None):
    from ..pipeline import DecoderResult
    result = parse_epub(raw, limits=limits, cancel_check=cancel_check)
    return DecoderResult(result.text, "epub", result.had_bom, result.encoding_evidence)


def decode_epub_with_evidence(raw: bytes, *, limits: ImportLimits = DEFAULT_LIMITS, cancel_check: Callable[[], bool] | None = None):
    result = parse_epub(raw, limits=limits, cancel_check=cancel_check)
    return decode_epub(raw, limits=limits, cancel_check=cancel_check), result.encoding_evidence
