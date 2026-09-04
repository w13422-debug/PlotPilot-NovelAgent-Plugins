"""Closed immutable style, qualification and lexicon contracts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    # SemVer 2.0.0: numeric prerelease identifiers may not contain leading
    # zeroes; identifiers containing a letter or hyphen are non-numeric.
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
STYLE_PACK_FIELDS = frozenset({"schema", "style_pack_id", "version", "style_release_id", "payload_hash",
    "source_cas", "target_cas", "manufacture_snapshot", "features", "lexicon_bindings", "constraints", "exemplar_hashes"})
QUALIFICATION_FIELDS = frozenset({"schema", "receipt_id", "style_pack_id", "style_release_id", "style_payload_hash",
    "qualification_run_snapshot_hash", "rubric_hash", "manufacturing_route_id", "writer_route_id", "reviewer_route_id",
    "manufacturing_attempt_ids", "writer_attempt_ids", "reviewer_attempt_ids", "case_results", "decision",
    "automatic_eligible", "created_at", "receipt_hash"})
# The original receipt profile remains accepted for immutable historical
# fixtures.  New qualification runs use the extended profile so every
# reviewed Asset is directly bound into the receipt rather than represented by
# an un-attributed caller supplied hash.
QUALIFICATION_EXTENDED_FIELDS = QUALIFICATION_FIELDS | frozenset({
    "style_pack_asset_id", "style_pack_asset_hash",
    "sealed_case_asset_id", "sealed_case_asset_hash",
    "rubric_asset_id", "rubric_asset_hash",
    "writer_output_asset_ids", "writer_output_asset_hashes",
    # The reviewer response is also immutable evidence.  Binding its Asset
    # identity prevents a later caller from replacing the score payload while
    # retaining an otherwise valid writer/case receipt.
    "review_output_asset_id", "review_output_asset_hash",
})
LEXICON_FIELDS = frozenset({"schema", "lexicon_id", "version", "normalization", "conflict_policy", "entries"})


class StyleContractError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    try:
        from plotpilot_plugin_sdk.canonical import canonical_bytes
        return bytes(canonical_bytes(value))
    except ImportError:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def hash_json(prefix: str, value: Any) -> str:
    return sha256_bytes(prefix.encode("ascii") + b"\n" + canonical_json(value))


def _exact(value: Mapping[str, Any], fields: frozenset[str] | set[str], label: str) -> None:
    if set(value) != set(fields):
        raise StyleContractError(f"{label} fields are not closed")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise StyleContractError(f"{label} must be a Core identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise StyleContractError(f"{label} must be lowercase SHA-256")
    return value


def style_payload_hash(pack: Mapping[str, Any]) -> str:
    projection = deepcopy(dict(pack)); projection["payload_hash"] = ""; projection["style_release_id"] = ""
    return hash_json("style-pack/v1", projection)


def style_release_id(style_pack_id: str, version: str, payload_hash: str) -> str:
    return sha256_bytes(f"style-release/v1\n{style_pack_id}\n{version}\n{payload_hash}\n".encode("utf-8"))


def validate_style_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(pack, Mapping): raise StyleContractError("style pack must be an object")
    value = deepcopy(dict(pack)); _exact(value, STYLE_PACK_FIELDS, "style pack")
    if value["schema"] != "style-pack/v1": raise StyleContractError("style pack schema mismatch")
    _identifier(value["style_pack_id"], "style_pack_id")
    if not isinstance(value["version"], str) or SEMVER_RE.fullmatch(value["version"]) is None:
        raise StyleContractError("style pack version must be SemVer")
    _hash(value["payload_hash"], "payload_hash"); _hash(value["style_release_id"], "style_release_id")
    source = value["source_cas"]; target = value["target_cas"]; snap = value["manufacture_snapshot"]
    if not isinstance(source, Mapping): raise StyleContractError("source_cas must be an object")
    _exact(source, {"asset_id", "sha256", "revision_id"}, "source_cas")
    _identifier(source["asset_id"], "source_cas.asset_id"); _hash(source["sha256"], "source_cas.sha256"); _identifier(source["revision_id"], "source_cas.revision_id")
    if not isinstance(target, Mapping): raise StyleContractError("target_cas must be an object")
    _exact(target, {"workspace_id", "entity_id", "base_revision_id", "base_content_hash"}, "target_cas")
    for field in ("workspace_id", "entity_id", "base_revision_id"): _identifier(target[field], f"target_cas.{field}")
    _hash(target["base_content_hash"], "target_cas.base_content_hash")
    if not isinstance(snap, Mapping): raise StyleContractError("manufacture_snapshot must be an object")
    _exact(snap, {"run_snapshot_hash", "route_id", "prompt_hash", "output_schema_hash", "chunk_plan_hash", "provider_attempt_ids", "checkpoint_ids", "usage"}, "manufacture_snapshot")
    for field in ("run_snapshot_hash", "prompt_hash", "output_schema_hash", "chunk_plan_hash"): _hash(snap[field], f"manufacture_snapshot.{field}")
    _identifier(snap["route_id"], "manufacture_snapshot.route_id")
    for field in ("provider_attempt_ids", "checkpoint_ids"):
        rows = snap[field]
        if not isinstance(rows, list) or not rows or len(rows) != len(set(rows)): raise StyleContractError(f"{field} must be non-empty unique")
        for item in rows: _identifier(item, field)
    usage = snap["usage"]
    if not isinstance(usage, Mapping): raise StyleContractError("usage must be an object")
    _exact(usage, {"input_tokens", "output_tokens", "total_tokens"}, "usage")
    if any(isinstance(usage[k], bool) or not isinstance(usage[k], int) or usage[k] < 0 for k in usage): raise StyleContractError("usage counts must be non-negative integers")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]: raise StyleContractError("usage total mismatch")
    features = value["features"]
    if not isinstance(features, Mapping): raise StyleContractError("features must be an object")
    _exact(features, {"voice", "rhythm", "syntax", "imagery", "taboos"}, "features")
    for field, rows in features.items():
        if not isinstance(rows, list) or any(not isinstance(x, str) or not x for x in rows) or rows != sorted(set(rows), key=lambda x: x.encode("utf-8")):
            raise StyleContractError(f"features.{field} must be byte-sorted unique strings")
    bindings = value["lexicon_bindings"]
    if not isinstance(bindings, list): raise StyleContractError("lexicon_bindings must be an array")
    orders = []
    for item in bindings:
        if not isinstance(item, Mapping): raise StyleContractError("lexicon binding must be an object")
        _exact(item, {"data_plugin_id", "data_release_id", "bundle_hash", "order"}, "lexicon binding")
        _identifier(item["data_plugin_id"], "data_plugin_id"); _hash(item["data_release_id"], "data_release_id"); _hash(item["bundle_hash"], "bundle_hash")
        if isinstance(item["order"], bool) or not isinstance(item["order"], int) or item["order"] < 1: raise StyleContractError("lexicon order invalid")
        orders.append(item["order"])
    if orders != list(range(1, len(orders)+1)): raise StyleContractError("lexicon order must be contiguous")
    for field in ("constraints",):
        rows=value[field]
        if not isinstance(rows,list) or rows != sorted(set(rows), key=lambda x:x.encode("utf-8")) or any(not isinstance(x,str) or not x for x in rows): raise StyleContractError(f"{field} invalid")
    hashes=value["exemplar_hashes"]
    if not isinstance(hashes,list) or not hashes or hashes != sorted(set(hashes)):
        raise StyleContractError("exemplar_hashes must be non-empty sorted unique")
    for item in hashes: _hash(item,"exemplar_hash")
    observed = style_payload_hash(value)
    if observed != value["payload_hash"]: raise StyleContractError("style payload_hash mismatch")
    if style_release_id(value["style_pack_id"], value["version"], observed) != value["style_release_id"]: raise StyleContractError("style release identity mismatch")
    return value


def _attempts(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) != len(set(value)): raise StyleContractError(f"{label} must be non-empty unique")
    for item in value: _identifier(item, label)
    return list(value)


def build_qualification_receipt(*, receipt_id: str, style_pack: Mapping[str, Any], qualification_run_snapshot_hash: str,
                                rubric_hash: str, manufacturing_route_id: str, writer_route_id: str,
                                reviewer_route_id: str, manufacturing_attempt_ids: list[str], writer_attempt_ids: list[str],
                                reviewer_attempt_ids: list[str], case_results: list[Mapping[str, Any]], created_at: str,
                                style_pack_asset_id: str | None = None, style_pack_asset_hash: str | None = None,
                                sealed_case_asset_id: str | None = None, sealed_case_asset_hash: str | None = None,
                                 rubric_asset_id: str | None = None, rubric_asset_hash: str | None = None,
                                 writer_output_asset_ids: list[str] | None = None,
                                 writer_output_asset_hashes: list[str] | None = None,
                                 review_output_asset_id: str | None = None,
                                 review_output_asset_hash: str | None = None) -> dict[str, Any]:
    pack=validate_style_pack(style_pack)
    cases=[deepcopy(dict(x)) for x in case_results]
    decision="pass" if len(cases)==3 and all(x.get("passed") is True for x in cases) else "fail"
    value={"schema":"author-style-qualification-receipt/v1","receipt_id":receipt_id,
           "style_pack_id":pack["style_pack_id"],"style_release_id":pack["style_release_id"],"style_payload_hash":pack["payload_hash"],
           "qualification_run_snapshot_hash":qualification_run_snapshot_hash,"rubric_hash":rubric_hash,
           "manufacturing_route_id":manufacturing_route_id,"writer_route_id":writer_route_id,"reviewer_route_id":reviewer_route_id,
           "manufacturing_attempt_ids":list(manufacturing_attempt_ids),"writer_attempt_ids":list(writer_attempt_ids),
           "reviewer_attempt_ids":list(reviewer_attempt_ids),"case_results":cases,"decision":decision,
           "automatic_eligible":decision=="pass","created_at":created_at,"receipt_hash":""}
    bindings = (style_pack_asset_id, style_pack_asset_hash, sealed_case_asset_id,
                sealed_case_asset_hash, rubric_asset_id, rubric_asset_hash,
                 writer_output_asset_ids, writer_output_asset_hashes,
                 review_output_asset_id, review_output_asset_hash)
    if any(item is not None for item in bindings):
        if not all(item is not None for item in bindings):
            raise StyleContractError("qualification Asset bindings must be complete")
        value.update({
            "style_pack_asset_id": _identifier(style_pack_asset_id, "style_pack_asset_id"),
            "style_pack_asset_hash": _hash(style_pack_asset_hash, "style_pack_asset_hash"),
            "sealed_case_asset_id": _identifier(sealed_case_asset_id, "sealed_case_asset_id"),
            "sealed_case_asset_hash": _hash(sealed_case_asset_hash, "sealed_case_asset_hash"),
            "rubric_asset_id": _identifier(rubric_asset_id, "rubric_asset_id"),
            "rubric_asset_hash": _hash(rubric_asset_hash, "rubric_asset_hash"),
            "writer_output_asset_ids": list(writer_output_asset_ids or []),
            "writer_output_asset_hashes": list(writer_output_asset_hashes or []),
            "review_output_asset_id": _identifier(review_output_asset_id, "review_output_asset_id"),
            "review_output_asset_hash": _hash(review_output_asset_hash, "review_output_asset_hash"),
        })
    value["receipt_hash"]=hash_json("author-style-qualification-receipt/v1", {k:v for k,v in value.items() if k!="receipt_hash"})
    return validate_qualification_receipt(
        value,
        expected_style_release_id=pack["style_release_id"],
        expected_style_pack=pack,
    )


def validate_qualification_receipt(
    receipt: Mapping[str, Any],
    expected_style_release_id: str | None = None,
    *,
    expected_style_pack: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping): raise StyleContractError("qualification receipt must be an object")
    value=deepcopy(dict(receipt))
    if set(value) == set(QUALIFICATION_FIELDS):
        pass
    elif set(value) == set(QUALIFICATION_EXTENDED_FIELDS):
        for field in ("style_pack_asset_id", "sealed_case_asset_id", "rubric_asset_id"):
            _identifier(value[field], field)
        for field in ("style_pack_asset_hash", "sealed_case_asset_hash", "rubric_asset_hash"):
            _hash(value[field], field)
        if (not isinstance(value["writer_output_asset_ids"], list)
                or not isinstance(value["writer_output_asset_hashes"], list)
                or len(value["writer_output_asset_ids"]) != len(value["writer_output_asset_hashes"])
                or len(value["writer_output_asset_ids"]) != 3
                or len(set(value["writer_output_asset_ids"])) != 3
                or len(set(value["writer_output_asset_hashes"])) != 3):
            raise StyleContractError("writer output Asset bindings are not complete")
        for item in value["writer_output_asset_ids"]: _identifier(item, "writer_output_asset_id")
        for item in value["writer_output_asset_hashes"]: _hash(item, "writer_output_asset_hash")
        _identifier(value["review_output_asset_id"], "review_output_asset_id")
        _hash(value["review_output_asset_hash"], "review_output_asset_hash")
    else:
        raise StyleContractError("qualification receipt fields are not closed")
    if value["schema"]!="author-style-qualification-receipt/v1": raise StyleContractError("qualification schema mismatch")
    for f in ("receipt_id","style_pack_id","manufacturing_route_id","writer_route_id","reviewer_route_id"): _identifier(value[f],f)
    for f in ("style_release_id","style_payload_hash","qualification_run_snapshot_hash","rubric_hash","receipt_hash"): _hash(value[f],f)
    if expected_style_release_id is not None and value["style_release_id"] != expected_style_release_id: raise StyleContractError("qualification is for another style release")
    routes={value["manufacturing_route_id"],value["writer_route_id"],value["reviewer_route_id"]}
    if len(routes)!=3: raise StyleContractError("same-path qualification is forbidden")
    groups=[set(_attempts(value[f],f)) for f in ("manufacturing_attempt_ids","writer_attempt_ids","reviewer_attempt_ids")]
    if any(groups[i]&groups[j] for i in range(3) for j in range(i+1,3)): raise StyleContractError("qualification attempt paths overlap")
    cases=value["case_results"]
    if not isinstance(cases,list) or len(cases)!=3: raise StyleContractError("qualification requires exactly three cases")
    ids=[]
    for case in cases:
        if not isinstance(case,Mapping): raise StyleContractError("qualification case must be an object")
        _exact(case,{"case_id","writer_output_hash","review_output_hash","score_0_100","passed"},"qualification case")
        _identifier(case["case_id"],"case_id"); ids.append(case["case_id"])
        _hash(case["writer_output_hash"],"writer_output_hash"); _hash(case["review_output_hash"],"review_output_hash")
        score=case["score_0_100"]
        if isinstance(score,bool) or not isinstance(score,int) or not 0<=score<=100 or not isinstance(case["passed"],bool): raise StyleContractError("qualification case score/status invalid")
    if len(ids)!=len(set(ids)): raise StyleContractError("qualification case IDs must be unique")
    passed=all(case["passed"] for case in cases)
    if value["decision"] not in {"pass","fail"} or (value["decision"]=="pass") != passed: raise StyleContractError("qualification decision mismatch")
    if not isinstance(value["automatic_eligible"],bool) or value["automatic_eligible"] != passed: raise StyleContractError("automatic eligibility mismatch")
    expected=hash_json("author-style-qualification-receipt/v1", {k:v for k,v in value.items() if k!="receipt_hash"})
    if value["receipt_hash"] != expected: raise StyleContractError("qualification receipt hash mismatch")
    if expected_style_pack is not None:
        pack = validate_style_pack(expected_style_pack)
        if value["style_pack_id"] != pack["style_pack_id"] or value["style_release_id"] != pack["style_release_id"] or value["style_payload_hash"] != pack["payload_hash"]:
            raise StyleContractError("qualification is not bound to the exact style pack")
        manufacture = pack["manufacture_snapshot"]
        if value["manufacturing_route_id"] != manufacture["route_id"]:
            raise StyleContractError("qualification manufacturing route differs from the exact style release")
        if not set(manufacture["provider_attempt_ids"]).issubset(set(value["manufacturing_attempt_ids"])):
            raise StyleContractError("qualification does not cover every frozen manufacturing attempt")
        if set(value) == set(QUALIFICATION_EXTENDED_FIELDS):
            # The Asset hash is the canonical bytes hash, and therefore must
            # agree with the exact pack passed by the caller.  IDs are checked
            # for presence/shape here; Core owns the Asset-to-ID lookup.
            if value["style_pack_asset_hash"] != sha256_bytes(canonical_json(pack)):
                raise StyleContractError("qualification style pack Asset hash is not exact")
    return value


def exact_release_eligible(style_pack: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    try:
        pack=validate_style_pack(style_pack)
        qual=validate_qualification_receipt(
            receipt,
            pack["style_release_id"],
            expected_style_pack=pack,
        )
        return bool(qual["automatic_eligible"] and qual["decision"]=="pass" and qual["style_pack_id"]==pack["style_pack_id"] and qual["style_payload_hash"]==pack["payload_hash"])
    except StyleContractError:
        return False


def validate_lexicon(lexicon: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(lexicon,Mapping): raise StyleContractError("lexicon must be an object")
    value=deepcopy(dict(lexicon)); _exact(value,LEXICON_FIELDS,"lexicon")
    if value["schema"]!="lexicon/v1" or value["normalization"]!="unicode-nfc-casefold-v1" or value["conflict_policy"]!="highest-priority-then-plugin-id-byte-order": raise StyleContractError("lexicon frozen identity/policy mismatch")
    _identifier(value["lexicon_id"],"lexicon_id")
    if not isinstance(value["version"],str) or SEMVER_RE.fullmatch(value["version"]) is None: raise StyleContractError("lexicon version invalid")
    entries=value["entries"]
    if not isinstance(entries,list): raise StyleContractError("lexicon entries must be an array")
    keys=[]
    for row in entries:
        if not isinstance(row,Mapping): raise StyleContractError("lexicon entry must be an object")
        _exact(row,{"term","normalized_term","preferred","aliases","forbidden","note","priority","source_plugin_id"},"lexicon entry")
        for f in ("term","normalized_term","preferred","note"):
            if not isinstance(row[f],str) or (f!="note" and not row[f]): raise StyleContractError(f"lexicon {f} invalid")
        _identifier(row["source_plugin_id"],"source_plugin_id")
        aliases = row["aliases"]
        if (not isinstance(aliases, list)
                or any(not isinstance(alias, str) or not alias for alias in aliases)
                or aliases != sorted(set(aliases), key=lambda x: x.encode("utf-8"))):
            raise StyleContractError("aliases must be non-empty sorted unique strings")
        if not isinstance(row["forbidden"],bool) or isinstance(row["priority"],bool) or not isinstance(row["priority"],int): raise StyleContractError("lexicon flag/priority invalid")
        keys.append(row["normalized_term"])
    if keys!=sorted(set(keys),key=lambda x:x.encode("utf-8")): raise StyleContractError("lexicon entries must be normalized-key sorted unique")
    return value


def merge_lexicons(lexicons: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values=[validate_lexicon(x) for x in lexicons]
    if not values: raise StyleContractError("at least one lexicon is required")
    candidates: dict[str,list[dict[str,Any]]]={}
    for value in values:
        for row in value["entries"]: candidates.setdefault(row["normalized_term"],[]).append(deepcopy(row))
    merged=[]
    for key,rows in candidates.items():
        rows.sort(key=lambda x:(-x["priority"],x["source_plugin_id"].encode("utf-8"),canonical_json(x)))
        merged.append(rows[0])
    merged.sort(key=lambda x:x["normalized_term"].encode("utf-8"))
    digest=hash_json("lexicon-merge/v1", [{"lexicon_id":x["lexicon_id"],"version":x["version"],"entries":x["entries"]} for x in sorted(values,key=lambda x:x["lexicon_id"].encode("utf-8"))])
    return validate_lexicon({"schema":"lexicon/v1","lexicon_id":f"merged:{digest[:48]}","version":"1.0.0",
        "normalization":"unicode-nfc-casefold-v1","conflict_policy":"highest-priority-then-plugin-id-byte-order","entries":merged})
