"""Pure, closed B1 source-structure and EvidenceSpan contracts."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes_sdk
    from plotpilot_plugin_sdk.canonical import sha256_hex as _sha256_sdk
    from plotpilot_plugin_sdk.verifier import verify_evidence_span as _verify_span_sdk
except ImportError as exc:
    _SDK_IMPORT_ERROR = exc
    _canonical_bytes_sdk = None
    _sha256_sdk = None
    _verify_span_sdk = None
else:
    _SDK_IMPORT_ERROR = None

PLUGIN_ID = "com.plotpilot.novelagent.source-structure"
VERSION = "0.1.0"
CLASSIFICATIONS = ("unchanged", "rebound", "needs_rerun", "orphaned")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
SPAN_FIELDS = frozenset({"schema","workspace_id","document_id","revision_id","node_id","start_codepoint","end_codepoint","quote","quote_hash","canonical_text_hash"})
NODE_FIELDS = frozenset({"schema","node_id","node_kind","title","start_codepoint","end_codepoint","body_start_codepoint","body_end_codepoint","parent_node_id","ordinal","removed"})
EVIDENCE_FIELDS = frozenset({"schema","evidence_id","span","parent_candidate_id"})
OPERATION_FIELDS = {
    "rename": ({"schema","operation_id","type","node_id","title"},{"operation_id","type","node_id","title"},{"type","node_id","title"}),
    "set_removed": ({"schema","operation_id","type","node_id","removed"},{"operation_id","type","node_id","removed"},{"type","node_id","removed"}),
    "set_type": ({"schema","operation_id","type","node_id","node_kind"},{"operation_id","type","node_id","node_kind"},{"type","node_id","node_kind"}),
    "move": ({"schema","operation_id","type","node_id","parent_node_id","ordinal"},{"operation_id","type","node_id","parent_node_id","ordinal"},{"type","node_id","parent_node_id","ordinal"}),
    "reorder": ({"schema","operation_id","type","node_id","ordinal"},{"operation_id","type","node_id","ordinal"},{"type","node_id","ordinal"}),
    "split": ({"schema","operation_id","type","node_id","boundaries"},{"schema","operation_id","type","node_id","boundaries","titles"},{"operation_id","type","node_id","boundaries"},{"operation_id","type","node_id","boundaries","titles"},{"type","node_id","boundaries"},{"type","node_id","boundaries","titles"}),
    "merge": ({"schema","operation_id","type","node_ids"},{"schema","operation_id","type","node_ids","title"},{"operation_id","type","node_ids"},{"operation_id","type","node_ids","title"},{"type","node_ids"},{"type","node_ids","title"}),
}


class StructureContractError(ValueError):
    """Fail-closed structure contract violation."""


class EvidenceSpanError(StructureContractError):
    """Fail-closed EvidenceSpan violation."""


def _require_sdk() -> None:
    if _canonical_bytes_sdk is None or _sha256_sdk is None or _verify_span_sdk is None:
        detail = str(_SDK_IMPORT_ERROR or "public PlotPilot SDK is unavailable")
        raise StructureContractError("public PlotPilot SDK is unavailable: " + detail)


def _bytes(value: Any) -> bytes:
    _require_sdk()
    return bytes(_canonical_bytes_sdk(value))


def _sha(data: bytes) -> str:
    _require_sdk()
    return str(_sha256_sdk(data))


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise StructureContractError("text must be a string")
    try:
        return _sha(value.encode("utf-8","strict"))
    except UnicodeEncodeError as exc:
        raise StructureContractError("text contains an invalid Unicode scalar") from exc


def hash_json(domain: str, value: Any) -> str:
    if not isinstance(domain, str) or not domain:
        raise StructureContractError("hash domain must be non-empty")
    return _sha(domain.encode("utf-8") + b"\n" + _bytes(value))


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise StructureContractError(f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise StructureContractError(f"{label} must be lowercase SHA-256")
    return value


def _int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StructureContractError(f"{label} must be an integer >= {minimum}")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise StructureContractError(f"{label} must be a boolean")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StructureContractError(f"{label} must be a string")
    try:
        value.encode("utf-8","strict")
    except UnicodeEncodeError as exc:
        raise StructureContractError(f"{label} contains an invalid Unicode scalar") from exc
    return value


def _closed(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StructureContractError(f"{label} must be an object")
    actual = set(value)
    if actual != set(fields):
        raise StructureContractError(f"{label} fields are not closed: missing={sorted(set(fields)-actual)}, extra={sorted(actual-set(fields))}")
    return dict(value)


def _span_range(start: Any, end: Any, label: str, nonempty: bool = False) -> tuple[int,int]:
    first, last = _int(start, f"{label}.start_codepoint"), _int(end, f"{label}.end_codepoint")
    if last < first or (nonempty and first == last):
        raise StructureContractError(f"{label} must be start-inclusive/end-exclusive")
    return first, last


def _node_range(node: Mapping[str, Any]) -> dict[str,int]:
    return {"start_codepoint": int(node["start_codepoint"]), "end_codepoint": int(node["end_codepoint"])}


def build_evidence_span(*, workspace_id: str, document_id: str, revision_id: str, node_id: str, canonical_text: str, start_codepoint: int, end_codepoint: int, node_range: Mapping[str,int], canonical_text_hash: str|None = None) -> dict[str,Any]:
    for value, label in ((workspace_id,"workspace_id"),(document_id,"document_id"),(revision_id,"revision_id"),(node_id,"node_id")):
        _id(value,label)
    text = _text(canonical_text,"canonical_text")
    start, end = _span_range(start_codepoint,end_codepoint,"EvidenceSpan",True)
    if end > len(text):
        raise EvidenceSpanError("EvidenceSpan range exceeds canonical text scalar length")
    if not isinstance(node_range, Mapping) or set(node_range) != {"start_codepoint","end_codepoint"}:
        raise EvidenceSpanError("node_range must contain exactly start_codepoint/end_codepoint")
    node_start, node_end = _span_range(node_range["start_codepoint"],node_range["end_codepoint"],"node_range")
    if start < node_start or end > node_end:
        raise EvidenceSpanError("EvidenceSpan is outside its Node range")
    content_hash = canonical_text_hash or sha256_text(text)
    _hash(content_hash,"canonical_text_hash")
    quote = text[start:end]
    result = {"schema":"evidence-span/v1","workspace_id":workspace_id,"document_id":document_id,"revision_id":revision_id,"node_id":node_id,"start_codepoint":start,"end_codepoint":end,"quote":quote,"quote_hash":_sha(quote.encode("utf-8")),"canonical_text_hash":content_hash}
    return validate_evidence_span(result,text,node_range)


def validate_evidence_span(span: Mapping[str,Any], canonical_text: str|None = None, node_range: Mapping[str,int]|None = None, *, expected_workspace_id: str|None = None, expected_document_id: str|None = None, expected_revision_id: str|None = None, expected_node_id: str|None = None, expected_canonical_text_hash: str|None = None) -> dict[str,Any]:
    value = _closed(span,SPAN_FIELDS,"EvidenceSpan")
    if value["schema"] != "evidence-span/v1":
        raise EvidenceSpanError("EvidenceSpan schema mismatch")
    for field in ("workspace_id","document_id","revision_id","node_id"):
        _id(value[field],f"EvidenceSpan.{field}")
    for field, expected in (("workspace_id",expected_workspace_id),("document_id",expected_document_id),("revision_id",expected_revision_id),("node_id",expected_node_id)):
        if expected is not None and value[field] != expected:
            raise EvidenceSpanError(f"EvidenceSpan {field} does not match expected identity")
    start, end = _span_range(value["start_codepoint"],value["end_codepoint"],"EvidenceSpan",True)
    quote = _text(value["quote"],"EvidenceSpan.quote")
    _hash(value["quote_hash"],"EvidenceSpan.quote_hash")
    _hash(value["canonical_text_hash"],"EvidenceSpan.canonical_text_hash")
    if _sha(quote.encode("utf-8")) != value["quote_hash"]:
        raise EvidenceSpanError("EvidenceSpan quote_hash does not match UTF-8 quote bytes")
    if expected_canonical_text_hash is not None:
        _hash(expected_canonical_text_hash,"expected_canonical_text_hash")
        if value["canonical_text_hash"] != expected_canonical_text_hash:
            raise EvidenceSpanError("EvidenceSpan canonical_text_hash does not match the Revision")
    text = None
    if canonical_text is not None:
        text = _text(canonical_text,"canonical_text")
        if end > len(text) or text[start:end] != quote:
            raise EvidenceSpanError("EvidenceSpan quote is not the exact canonical scalar slice")
        if sha256_text(text) != value["canonical_text_hash"]:
            raise EvidenceSpanError("EvidenceSpan canonical_text_hash does not match canonical text")
    if node_range is not None:
        if not isinstance(node_range, Mapping) or set(node_range) != {"start_codepoint","end_codepoint"}:
            raise EvidenceSpanError("node_range must contain exactly start_codepoint/end_codepoint")
        ns, ne = _span_range(node_range["start_codepoint"],node_range["end_codepoint"],"node_range")
        if start < ns or end > ne:
            raise EvidenceSpanError("EvidenceSpan is outside its Node range")
    _require_sdk()
    try:
        _verify_span_sdk(value,text,node_range,expected_workspace_id=expected_workspace_id,expected_document_id=expected_document_id,expected_revision_id=expected_revision_id,expected_node_id=expected_node_id,expected_canonical_text_hash=expected_canonical_text_hash)
    except Exception as exc:
        raise EvidenceSpanError(str(exc)) from exc
    return value

def _canonical_node(node: Mapping[str,Any], default_ordinal: int = 0) -> dict[str,Any]:
    if not isinstance(node, Mapping):
        raise StructureContractError("Node must be an object")
    if set(node) == set(NODE_FIELDS):
        value = dict(node)
    else:
        aliases = {"schema","node_id","node_kind","title","start_codepoint","end_codepoint","body_start_codepoint","body_end_codepoint","parent_node_id","ordinal","removed","id","kind","start","end","body_start","body_end","parent_id","ord","discarded"}
        if not set(node).issubset(aliases):
            raise StructureContractError(f"Node fields are not recognized: {sorted(set(node)-aliases)}")
        value = {
            "schema":"source-structure-node/v1",
            "node_id":node.get("node_id",node.get("id")),
            "node_kind":node.get("node_kind",node.get("kind","node")),
            "title":node.get("title",""),
            "start_codepoint":node.get("start_codepoint",node.get("start")),
            "end_codepoint":node.get("end_codepoint",node.get("end")),
            "body_start_codepoint":node.get("body_start_codepoint",node.get("body_start",node.get("start_codepoint",node.get("start")))),
            "body_end_codepoint":node.get("body_end_codepoint",node.get("body_end",node.get("end_codepoint",node.get("end")))),
            "parent_node_id":node.get("parent_node_id",node.get("parent_id")),
            "ordinal":node.get("ordinal",node.get("ord",default_ordinal)),
            "removed":node.get("removed",node.get("discarded",False)),
        }
    if value["schema"] != "source-structure-node/v1":
        raise StructureContractError("Node schema mismatch")
    _id(value["node_id"],"Node.node_id")
    kind = _text(value["node_kind"],"Node.node_kind")
    if not kind:
        raise StructureContractError("Node.node_kind must be non-empty")
    _text(value["title"],"Node.title")
    start, end = _span_range(value["start_codepoint"],value["end_codepoint"],"Node")
    body_start, body_end = _span_range(value["body_start_codepoint"],value["body_end_codepoint"],"Node body")
    if body_start < start or body_end > end:
        raise StructureContractError("Node body range must be inside Node range")
    parent = value["parent_node_id"]
    if parent is not None:
        _id(parent,"Node.parent_node_id")
    _int(value["ordinal"],"Node.ordinal")
    removed = _bool(value["removed"],"Node.removed")
    return {"schema":"source-structure-node/v1","node_id":value["node_id"],"node_kind":kind,"title":value["title"],"start_codepoint":start,"end_codepoint":end,"body_start_codepoint":body_start,"body_end_codepoint":body_end,"parent_node_id":parent,"ordinal":int(value["ordinal"]),"removed":removed}


def validate_nodes(nodes: Iterable[Mapping[str,Any]], *, text_length: int|None = None) -> list[dict[str,Any]]:
    if not isinstance(nodes,(list,tuple)):
        raise StructureContractError("nodes must be an array")
    result = [_canonical_node(node,index) for index,node in enumerate(nodes)]
    ids = [node["node_id"] for node in result]
    if len(ids) != len(set(ids)):
        raise StructureContractError("Node IDs must be unique")
    if text_length is not None:
        _int(text_length,"text_length")
        if any(node["end_codepoint"] > text_length for node in result):
            raise StructureContractError("Node range exceeds canonical text")
    by_id = {node["node_id"]:node for node in result}
    for node in result:
        parent = node["parent_node_id"]
        if parent is not None and parent not in by_id:
            raise StructureContractError("Node parent does not exist")
        seen:set[str] = set()
        current: str|None = node["node_id"]
        while current is not None:
            if current in seen:
                raise StructureContractError("Node tree contains a cycle")
            seen.add(current)
            current = by_id[current]["parent_node_id"]
    return sorted(result,key=lambda item:(item["start_codepoint"],item["end_codepoint"],item["ordinal"],item["node_id"]))


def _operation(raw: Mapping[str,Any]) -> dict[str,Any]:
    if not isinstance(raw,Mapping):
        raise StructureContractError("structure operation must be an object")
    raw = dict(raw)
    kind = raw.get("type")
    if kind not in OPERATION_FIELDS or set(raw) not in OPERATION_FIELDS[kind]:
        raise StructureContractError(f"operation {kind!r} is not closed")
    if "schema" in raw and raw["schema"] != "source-structure-operation/v1":
        raise StructureContractError("structure operation schema mismatch")
    op_id = raw.get("operation_id","op-"+hash_json("source-structure-operation/v1",raw)[:40])
    _id(op_id,"operation_id")
    result:dict[str,Any] = {"schema":"source-structure-operation/v1","operation_id":op_id,"type":kind}
    if kind in {"rename","set_removed","set_type","move","reorder","split"}:
        _id(raw["node_id"],"operation.node_id")
        result["node_id"] = raw["node_id"]
    if kind == "rename":
        title = _text(raw["title"],"operation.title")
        if not title: raise StructureContractError("rename title must be non-empty")
        result["title"] = title
    elif kind == "set_removed":
        result["removed"] = _bool(raw["removed"],"operation.removed")
    elif kind == "set_type":
        result["node_kind"] = _text(raw["node_kind"],"operation.node_kind")
    elif kind in {"move","reorder"}:
        result["ordinal"] = _int(raw["ordinal"],"operation.ordinal")
        if kind == "move":
            parent = raw["parent_node_id"]
            if parent is not None: _id(parent,"operation.parent_node_id")
            result["parent_node_id"] = parent
    elif kind == "split":
        boundaries = raw["boundaries"]
        if not isinstance(boundaries,list) or not boundaries:
            raise StructureContractError("split boundaries must be a non-empty array")
        boundaries = [_int(item,"operation.boundary") for item in boundaries]
        if boundaries != sorted(set(boundaries)):
            raise StructureContractError("split boundaries must be strictly increasing")
        result["boundaries"] = boundaries
        if "titles" in raw:
            titles = raw["titles"]
            if not isinstance(titles,list) or len(titles) != len(boundaries)+1:
                raise StructureContractError("split titles must match segment count")
            result["titles"] = [_text(item,"operation.title") for item in titles]
    else:
        node_ids = raw["node_ids"]
        if not isinstance(node_ids,list) or len(node_ids) < 2:
            raise StructureContractError("merge node_ids must contain at least two nodes")
        result["node_ids"] = [_id(item,"operation.node_id") for item in node_ids]
        if len(result["node_ids"]) != len(set(result["node_ids"])):
            raise StructureContractError("merge node_ids must be unique")
        if "title" in raw:
            result["title"] = _text(raw["title"],"operation.title")
    return result


def _renumber(nodes:list[dict[str,Any]], parent:str|None) -> None:
    siblings = [node for node in nodes if node["parent_node_id"] == parent and not node["removed"]]
    siblings.sort(key=lambda item:(item["ordinal"],item["start_codepoint"],item["end_codepoint"],item["node_id"]))
    for ordinal,node in enumerate(siblings): node["ordinal"] = ordinal


def apply_structure_operations(nodes: Iterable[Mapping[str,Any]], operations: Iterable[Mapping[str,Any]], *, text_length: int|None = None) -> list[dict[str,Any]]:
    current = validate_nodes(nodes,text_length=text_length)
    if not isinstance(operations,(list,tuple)):
        raise StructureContractError("operations must be an array")
    for raw in operations:
        op = _operation(raw)
        by_id = {node["node_id"]:node for node in current}
        kind = op["type"]
        if kind in {"rename","set_removed","set_type","move","reorder","split"}:
            node = by_id.get(op["node_id"])
            if node is None: raise StructureContractError("operation refers to an unknown Node")
        if kind == "rename": node["title"] = op["title"]
        elif kind == "set_removed": node["removed"] = op["removed"]
        elif kind == "set_type": node["node_kind"] = op["node_kind"]
        elif kind == "reorder":
            node["ordinal"] = op["ordinal"]; _renumber(current,node["parent_node_id"])
        elif kind == "move":
            parent = op["parent_node_id"]
            if parent == node["node_id"]: raise StructureContractError("Node cannot be its own parent")
            if parent is not None and parent not in by_id: raise StructureContractError("move parent does not exist")
            ancestor = parent
            while ancestor is not None:
                if ancestor == node["node_id"]: raise StructureContractError("move would create a Node cycle")
                ancestor = by_id[ancestor]["parent_node_id"]
            old_parent = node["parent_node_id"]; node["parent_node_id"] = parent; node["ordinal"] = op["ordinal"]
            _renumber(current,old_parent); _renumber(current,parent)
        elif kind == "split":
            if node["removed"]: raise StructureContractError("cannot split a removed Node")
            if any(boundary <= node["body_start_codepoint"] or boundary >= node["body_end_codepoint"] for boundary in op["boundaries"]):
                raise StructureContractError("split boundary is outside the Node body")
            cuts = [node["start_codepoint"],*op["boundaries"],node["end_codepoint"]]
            titles = op.get("titles")
            generated:list[dict[str,Any]] = []
            for index,(start,end) in enumerate(zip(cuts,cuts[1:]),1):
                child_id = f"{node['node_id']}:split:{index}"
                if child_id in by_id: raise StructureContractError("deterministic split Node ID already exists")
                generated.append({"schema":"source-structure-node/v1","node_id":child_id,"node_kind":node["node_kind"],"title":titles[index-1] if titles else f"{node['title']} [{index}]","start_codepoint":start,"end_codepoint":end,"body_start_codepoint":max(node["body_start_codepoint"],start),"body_end_codepoint":min(node["body_end_codepoint"],end),"parent_node_id":node["parent_node_id"],"ordinal":node["ordinal"]+index-1,"removed":False})
            node["removed"] = True; current.extend(generated); _renumber(current,node["parent_node_id"])
        elif kind == "merge":
            selected = [by_id.get(node_id) for node_id in op["node_ids"]]
            if any(item is None for item in selected): raise StructureContractError("merge refers to an unknown Node")
            live = [item for item in selected if item is not None]
            if any(item["removed"] for item in live): raise StructureContractError("cannot merge a removed Node")
            if len({item["parent_node_id"] for item in live}) != 1: raise StructureContractError("merge Nodes must share a parent")
            live.sort(key=lambda item:(item["start_codepoint"],item["end_codepoint"],item["ordinal"],item["node_id"]))
            merged_id = f"{live[0]['node_id']}:merge:{hash_json('source-structure-merge/v1',op)[:20]}"
            if merged_id in by_id: raise StructureContractError("deterministic merge Node ID already exists")
            merged = {"schema":"source-structure-node/v1","node_id":merged_id,"node_kind":live[0]["node_kind"],"title":op.get("title") or " / ".join(item["title"] for item in live),"start_codepoint":min(item["start_codepoint"] for item in live),"end_codepoint":max(item["end_codepoint"] for item in live),"body_start_codepoint":min(item["body_start_codepoint"] for item in live),"body_end_codepoint":max(item["body_end_codepoint"] for item in live),"parent_node_id":live[0]["parent_node_id"],"ordinal":min(item["ordinal"] for item in live),"removed":False}
            for item in live: item["removed"] = True
            current.append(merged); _renumber(current,merged["parent_node_id"])
    return validate_nodes(current,text_length=text_length)

def _casefold_offsets(value: str) -> tuple[str,list[int]]:
    pieces:list[str] = []
    offsets:list[int] = []
    for index,char in enumerate(value):
        folded = char.casefold()
        pieces.append(folded)
        offsets.extend([index] * len(folded))
    return "".join(pieces),offsets


def _containing_node(nodes: Sequence[Mapping[str,Any]], start:int, end:int) -> dict[str,Any]|None:
    candidates = [dict(node) for node in nodes if not node["removed"] and node["start_codepoint"] <= start and node["end_codepoint"] >= end]
    if not candidates: return None
    return min(candidates,key=lambda item:(item["end_codepoint"]-item["start_codepoint"],item["start_codepoint"],item["ordinal"],item["node_id"]))


def find_text_matches(*, canonical_text:str, query:str, workspace_id:str, document_id:str, revision_id:str, canonical_text_hash:str|None=None, nodes:Iterable[Mapping[str,Any]]=(), case_sensitive:bool=True, max_matches:int=4096) -> list[dict[str,Any]]:
    text,query = _text(canonical_text,"canonical_text"),_text(query,"query")
    if not query: raise StructureContractError("query must be non-empty")
    for value,label in ((workspace_id,"workspace_id"),(document_id,"document_id"),(revision_id,"revision_id")): _id(value,label)
    _int(max_matches,"max_matches",1)
    text_hash = canonical_text_hash or sha256_text(text)
    _hash(text_hash,"canonical_text_hash")
    if text_hash != sha256_text(text): raise StructureContractError("canonical_text_hash does not match canonical text")
    node_list = validate_nodes(list(nodes),text_length=len(text)) if nodes else [{
        "schema":"source-structure-node/v1","node_id":"document-node","node_kind":"document","title":"",
        "start_codepoint":0,"end_codepoint":len(text),"body_start_codepoint":0,"body_end_codepoint":len(text),
        "parent_node_id":None,"ordinal":0,"removed":False,
    }]
    if case_sensitive:
        haystack,needle,offsets = text,query,list(range(len(text)))
    else:
        haystack,offsets = _casefold_offsets(text); needle = query.casefold()
    result:list[dict[str,Any]] = []
    seen_ranges:set[tuple[int,int]] = set()
    cursor = 0
    while len(result) < max_matches:
        found = haystack.find(needle,cursor)
        if found < 0: break
        end_folded = found + len(needle)
        if not offsets or end_folded > len(offsets): break
        start,end = offsets[found],offsets[end_folded-1]+1
        node = _containing_node(node_list,start,end)
        if node is not None and (start,end) not in seen_ranges:
            seen_ranges.add((start,end))
            span = build_evidence_span(workspace_id=workspace_id,document_id=document_id,revision_id=revision_id,node_id=node["node_id"],canonical_text=text,start_codepoint=start,end_codepoint=end,node_range=_node_range(node),canonical_text_hash=text_hash)
            result.append({"schema":"source-evidence-match/v1","match_id":"match-"+hash_json("source-evidence-match/v1",{"ordinal":len(result),"span":span})[:40],"ordinal":len(result),"span":span})
        cursor = max(found+1,end_folded)
    return result


def _evidence_item(value: Mapping[str,Any]) -> dict[str,Any]:
    item = _closed(value,EVIDENCE_FIELDS,"source evidence item")
    if item["schema"] != "source-evidence-item/v1": raise StructureContractError("source evidence item schema mismatch")
    _id(item["evidence_id"],"evidence_id")
    if item["parent_candidate_id"] is not None: _id(item["parent_candidate_id"],"parent_candidate_id")
    validate_evidence_span(item["span"])
    return item


def _rerun(evidence_id:str, source_span:Mapping[str,Any]|None, reason:str, count:int=0) -> dict[str,Any]:
    return {"schema":"source-evidence-rebind-item/v1","evidence_id":evidence_id,"classification":"needs_rerun","source_span":copy.deepcopy(dict(source_span)) if isinstance(source_span,Mapping) else None,"target_span":None,"reason":reason,"match_count":count,"candidate_eligible":False}


def rebind_evidence(
    evidence:Mapping[str,Any],
    source_text:str,
    target_text:str,
    source_nodes:Iterable[Mapping[str,Any]]=(),
    target_nodes:Iterable[Mapping[str,Any]]=(),
    *,
    source_canonical_text_hash:str|None=None,
    target_canonical_text_hash:str|None=None,
    target_revision_id:str|None=None,
    expected_workspace_id:str|None=None,
    expected_document_id:str|None=None,
    expected_source_revision_id:str|None=None,
) -> dict[str,Any]:
    """Classify an already-bound EvidenceSpan against one exact target Revision.

    Source lineage validation deliberately happens before classification.  A
    span from another workspace/document/revision/content hash is invalid
    input, not a ``needs_rerun`` classification.
    """
    if not isinstance(evidence,Mapping):
        raise StructureContractError("evidence must be an object")
    raw_span = evidence.get("span") if "span" in evidence else evidence
    evidence_id = str(evidence.get("evidence_id","evidence-"+hash_json("source-evidence-id/v1",raw_span)[:32]))
    _id(evidence_id,"evidence_id")
    if "span" in evidence:
        source_span = _evidence_item(evidence)["span"]
    elif isinstance(raw_span,Mapping):
        source_span = validate_evidence_span(raw_span)
    else:
        raise StructureContractError("evidence span must be an object")

    source,target = _text(source_text,"source_text"),_text(target_text,"target_text")
    source_hash = source_canonical_text_hash or sha256_text(source)
    target_hash = target_canonical_text_hash or sha256_text(target)
    _hash(source_hash,"source_canonical_text_hash")
    _hash(target_hash,"target_canonical_text_hash")
    if source_hash != sha256_text(source) or target_hash != sha256_text(target):
        raise EvidenceSpanError("rebind canonical text hash does not bind the supplied text")
    source_list = validate_nodes(list(source_nodes),text_length=len(source)) if source_nodes else []
    target_list = validate_nodes(list(target_nodes),text_length=len(target)) if target_nodes else []
    source_workspace = expected_workspace_id or source_span["workspace_id"]
    source_document = expected_document_id or source_span["document_id"]
    source_revision = expected_source_revision_id or source_span["revision_id"]
    for value,label in ((source_workspace,"workspace_id"),(source_document,"document_id"),(source_revision,"source_revision_id")):
        _id(value,label)
    source_node = next((node for node in source_list if node["node_id"] == source_span["node_id"]),None)
    if source_node is None:
        raise EvidenceSpanError("EvidenceSpan node_id is absent from the declared source Nodes")
    validate_evidence_span(
        source_span,
        source,
        _node_range(source_node),
        expected_workspace_id=source_workspace,
        expected_document_id=source_document,
        expected_revision_id=source_revision,
        expected_node_id=source_node["node_id"],
        expected_canonical_text_hash=source_hash,
    )

    target_revision = target_revision_id or source_revision
    _id(target_revision,"target_revision_id")
    if source_revision == target_revision:
        if source_hash != target_hash:
            raise EvidenceSpanError(
                "same Revision cannot change canonical_text_hash"
            )
        if source != target:
            raise EvidenceSpanError(
                "same Revision cannot change canonical text"
            )
        if source_list != target_list:
            raise EvidenceSpanError(
                "same Revision cannot change the canonical Node binding"
            )
        target_node = next(
            (
                node
                for node in target_list
                if node["node_id"] == source_span["node_id"]
            ),
            None,
        )
        if target_node is None:
            raise EvidenceSpanError(
                "same Revision lost the EvidenceSpan Node binding"
            )
        validate_evidence_span(
            source_span,
            target,
            _node_range(target_node),
            expected_workspace_id=source_workspace,
            expected_document_id=source_document,
            expected_revision_id=target_revision,
            expected_node_id=target_node["node_id"],
            expected_canonical_text_hash=target_hash,
        )
        return {"schema":"source-evidence-rebind-item/v1","evidence_id":evidence_id,"classification":"unchanged","source_span":copy.deepcopy(dict(source_span)),"target_span":copy.deepcopy(dict(source_span)),"reason":"same_revision_exact_noop","match_count":1,"candidate_eligible":False}

    quote = source_span["quote"]
    positions:list[int] = []
    cursor = 0
    while True:
        position = target.find(quote,cursor)
        if position < 0:
            break
        positions.append(position)
        cursor = position+1
    if not positions:
        return {"schema":"source-evidence-rebind-item/v1","evidence_id":evidence_id,"classification":"orphaned","source_span":copy.deepcopy(dict(source_span)),"target_span":None,"reason":"exact_quote_absent","match_count":0,"candidate_eligible":False}
    if len(positions) != 1:
        return _rerun(evidence_id,source_span,"exact_quote_not_unique",len(positions))
    position = positions[0]
    target_node = _containing_node(target_list,position,position+len(quote))
    if target_node is None:
        return _rerun(evidence_id,source_span,"exact_quote_has_no_containing_target_node",1)
    target_span = build_evidence_span(
        workspace_id=source_workspace,
        document_id=source_document,
        revision_id=target_revision,
        node_id=target_node["node_id"],
        canonical_text=target,
        start_codepoint=position,
        end_codepoint=position+len(quote),
        node_range=_node_range(target_node),
        canonical_text_hash=target_hash,
    )
    validate_evidence_span(
        target_span,
        target,
        _node_range(target_node),
        expected_workspace_id=source_workspace,
        expected_document_id=source_document,
        expected_revision_id=target_revision,
        expected_node_id=target_node["node_id"],
        expected_canonical_text_hash=target_hash,
    )
    return {"schema":"source-evidence-rebind-item/v1","evidence_id":evidence_id,"classification":"rebound","source_span":copy.deepcopy(dict(source_span)),"target_span":target_span,"reason":"unique_exact_quote_with_containment","match_count":1,"candidate_eligible":True}


def _validate_rebind_revision_gate(
    source_revision_id:str,
    source_canonical_text_hash:str,
    target_revision_id:str,
    target_canonical_text_hash:str,
    items:Sequence[Mapping[str,Any]],
) -> None:
    same_revision = source_revision_id == target_revision_id
    if same_revision and source_canonical_text_hash != target_canonical_text_hash:
        raise StructureContractError(
            "same Revision rebind report cannot change canonical_text_hash"
        )
    for item in items:
        classification = item["classification"]
        if same_revision:
            if classification != "unchanged":
                raise StructureContractError(
                    "same Revision rebind report may only contain unchanged"
                )
            if item["source_span"] != item["target_span"]:
                raise StructureContractError(
                    "same Revision unchanged spans must be an exact no-op"
                )
        elif classification == "unchanged":
            raise StructureContractError(
                "unchanged classification requires the same Revision"
            )


def build_rebind_report(*, workspace_id:str, document_id:str, source_revision_id:str, source_canonical_text_hash:str, target_revision_id:str, target_canonical_text_hash:str, items:Iterable[Mapping[str,Any]]) -> dict[str,Any]:
    for value,label in ((workspace_id,"workspace_id"),(document_id,"document_id"),(source_revision_id,"source_revision_id"),(target_revision_id,"target_revision_id")): _id(value,label)
    _hash(source_canonical_text_hash,"source_canonical_text_hash"); _hash(target_canonical_text_hash,"target_canonical_text_hash")
    normalized = [dict(item) for item in items]
    expected = frozenset({"schema","evidence_id","classification","source_span","target_span","reason","match_count","candidate_eligible"})
    ids:list[str] = []
    for item in normalized:
        if set(item) != set(expected): raise StructureContractError("rebind item fields are not closed")
        if item["schema"] != "source-evidence-rebind-item/v1": raise StructureContractError("rebind item schema mismatch")
        _id(item["evidence_id"],"rebind evidence_id")
        if item["classification"] not in CLASSIFICATIONS: raise StructureContractError("unknown rebind classification")
        if not isinstance(item["reason"],str) or not item["reason"]: raise StructureContractError("rebind reason must be non-empty")
        _int(item["match_count"],"rebind match_count"); _bool(item["candidate_eligible"],"rebind candidate_eligible")
        if item["candidate_eligible"] is not (item["classification"] == "rebound"): raise StructureContractError("candidate eligibility does not match classification")
        if item["classification"] in {"unchanged","rebound"} and item["target_span"] is None: raise StructureContractError("unchanged/rebound item requires target_span")
        if item["classification"] in {"needs_rerun","orphaned"} and item["target_span"] is not None: raise StructureContractError("needs_rerun/orphaned item cannot carry target_span")
        if item["source_span"] is not None:
            validate_evidence_span(item["source_span"],expected_workspace_id=workspace_id,expected_document_id=document_id,expected_revision_id=source_revision_id,expected_canonical_text_hash=source_canonical_text_hash)
        if item["target_span"] is not None:
            validate_evidence_span(item["target_span"],expected_workspace_id=workspace_id,expected_document_id=document_id,expected_revision_id=target_revision_id,expected_canonical_text_hash=target_canonical_text_hash)
        ids.append(item["evidence_id"])
    if len(ids) != len(set(ids)): raise StructureContractError("rebind evidence IDs must be unique")
    _validate_rebind_revision_gate(source_revision_id,source_canonical_text_hash,target_revision_id,target_canonical_text_hash,normalized)
    counts = {name:0 for name in CLASSIFICATIONS}
    for item in normalized: counts[item["classification"]] += 1
    body = {"schema":"source-evidence-rebind-report/v1","workspace_id":workspace_id,"document_id":document_id,"source_revision_id":source_revision_id,"source_canonical_text_hash":source_canonical_text_hash,"target_revision_id":target_revision_id,"target_canonical_text_hash":target_canonical_text_hash,"items":normalized,"counts":counts}
    return {**body,"report_hash":hash_json("source-evidence-rebind-report/v1",body)}


def validate_rebind_report(report:Mapping[str,Any]) -> dict[str,Any]:
    fields = frozenset({"schema","workspace_id","document_id","source_revision_id","source_canonical_text_hash","target_revision_id","target_canonical_text_hash","items","counts","report_hash"})
    value = _closed(report,fields,"rebind report")
    if value["schema"] != "source-evidence-rebind-report/v1" or not isinstance(value["items"],list): raise StructureContractError("rebind report schema/items are invalid")
    counts = {name:0 for name in CLASSIFICATIONS}
    for item in value["items"]: counts[item["classification"]] += 1
    if value["counts"] != counts: raise StructureContractError("rebind report counts do not match items")
    _validate_rebind_revision_gate(value["source_revision_id"],value["source_canonical_text_hash"],value["target_revision_id"],value["target_canonical_text_hash"],value["items"])
    _hash(value["report_hash"],"report_hash")
    body = dict(value); del body["report_hash"]
    if value["report_hash"] != hash_json("source-evidence-rebind-report/v1",body): raise StructureContractError("rebind report hash mismatch")
    return value


__all__ = ["CLASSIFICATIONS","EvidenceSpanError","StructureContractError","apply_structure_operations","build_evidence_span","build_rebind_report","find_text_matches","hash_json","rebind_evidence","sha256_text","validate_evidence_span","validate_nodes","validate_rebind_report"]
