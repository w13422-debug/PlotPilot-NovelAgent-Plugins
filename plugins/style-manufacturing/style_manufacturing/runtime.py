"""Host-bound G2 runtime for manufacture, independent qualification and packaging."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
from typing import Any, Mapping, Protocol

from .capability_spec import CAPABILITIES, NEEDS, PLUGIN_ID, SPEC_BY_CAPABILITY, VERSION, descriptors
from .contract import (StyleContractError, build_qualification_receipt, canonical_json,
    exact_release_eligible, hash_json, sha256_bytes, style_payload_hash, style_release_id,
    validate_qualification_receipt, validate_style_pack, SEMVER_RE)
from .package_identity import load_runtime_identity

try:
    from plotpilot_plugin_sdk.verifier import (validate_rpc_result as _validate_rpc_result_sdk,
        verify_checkpoint as _verify_checkpoint_sdk, verify_provenance_receipt as _verify_provenance_receipt_sdk,
        verify_result_bundle as _verify_result_bundle_sdk)
except ImportError as exc:  # pragma: no cover
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _validate_rpc_result_sdk = _verify_checkpoint_sdk = _verify_provenance_receipt_sdk = _verify_result_bundle_sdk = None
else:
    _SDK_IMPORT_ERROR = None

CAPABILITY_MANUFACTURE = "style.manufacture/v1"
CAPABILITY_QUALIFY = "style.qualify/v1"
CAPABILITY_PACKAGE = "style.data-plugin.package/v1"
_ID_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE=re.compile(r"^[0-9a-f]{64}$")
_TIME_RE=re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$")
_ZERO_HASH="0"*64; _PAGE_SIZE=8_388_608; _MAX_PAGES=4096
_QUALITY_TOKEN_RE = re.compile(r"[^\W_]+(?:['’/][^\W_]+)*", re.UNICODE)
_QUALITY_PLACEHOLDER_TOKENS = frozenset({
    "n/a", "na", "none", "null", "nil", "无", "无内容", "测试", "test",
    "testing", "placeholder", "sample", "example", "output", "foo", "bar", "foobar",
    "lorem", "ipsum", "dolor", "sit", "amet", "qwerty", "asdf", "zxcv",
    "bad", "blah", "dummy", "todo", "tbd",
})
_COMMON=frozenset({"schema","capability_id","operation_key","operation","job_id","step_id","attempt_id","worker_run_id",
    "lease_epoch","checkpoint_ids","provenance_receipt_id","created_at","total_units","run_snapshot_hash","workspace_id"})
_MANUFACTURE=frozenset({"style_pack_id","style_version","source_asset_id","source_asset_hash","source_revision_id",
    "target_entity_id","target_base_revision_id","target_base_content_hash","model_profile_revision_id","route_id",
    "prompt_hash","output_schema_hash","chunk_plan_hash","lexicon_asset_refs","provider_attempt_ids"})
_QUALIFY=frozenset({"style_pack_asset_id","style_pack_asset_hash","rubric_asset_id","rubric_asset_hash",
    "manufacturing_route_id","writer_route_id","reviewer_route_id","manufacturing_attempt_ids","writer_attempt_ids",
    "reviewer_attempt_ids","writer_model_profile_revision_id","reviewer_model_profile_revision_id"})
_PACKAGE=frozenset({"style_pack_asset_id","style_pack_asset_hash","qualification_receipt_asset_id",
    "qualification_receipt_asset_hash","data_plugin_id","data_version"})
# These fields are additive and optional so historical callers remain valid;
# when supplied they turn manufacturing into the fully materialized
# map/synthesis pipeline required by the v1 remediation.
_MANUFACTURE_OPTIONAL=frozenset({
    "chunk_plan_asset_id", "chunk_plan_asset_hash", "map_model_profile_revision_id",
    "synthesis_model_profile_revision_id", "map_attempt_ids", "synthesis_attempt_ids",
})
_QUALIFY_OPTIONAL=frozenset({
    "sealed_case_asset_id", "sealed_case_asset_hash",
    # Accepted aliases are intentionally normalized to the canonical pair.
    "sealed_cases_asset_id", "sealed_cases_asset_hash",
})
_RESUME=frozenset({"resume_checkpoint_asset_id","resume_checkpoint_asset_hash","resume_state_asset_id","resume_state_asset_hash"})
# The data namespace is checked against the complete frozen catalog rather
# than just the two Style code packages.  This is deliberately embedded in
# the worker: a package must remain collision-free when this wheel is used in
# isolation (without reading the mutable catalog at runtime).
_RESERVED_DATA_IDS=frozenset({
    # Frozen Code plugin IDs.
    "com.plotpilot.novelagent.source-import",
    "com.plotpilot.novelagent.source-cleaning-runtime",
    "com.plotpilot.novelagent.source-structure",
    "com.plotpilot.novelagent.donor-analysis",
    "com.plotpilot.novelagent.narrative-analysis",
    "com.plotpilot.novelagent.character-distillation",
    "com.plotpilot.novelagent.asset-derivation",
    "com.plotpilot.novelagent.style-manufacturing",
    "com.plotpilot.novelagent.style-runtime",
    "com.plotpilot.novelagent.consistency-audit",
    "com.plotpilot.novelagent.impact-repair",
    "com.plotpilot.novelagent.branch-canon",
    "com.plotpilot.novelagent.story-state",
    "com.plotpilot.novelagent.writing-context",
    "com.plotpilot.novelagent.chapter-workflow",
    "com.plotpilot.novelagent.skill-recommender",
    "com.plotpilot.novelagent.analysis-io",
    "com.plotpilot.novelagent.outline-projection",
    "com.plotpilot.novelagent.knowledge-projection",
    "com.plotpilot.novelagent.provider-openai-compatible",
    "com.plotpilot.novelagent.provider-anthropic",
    "com.plotpilot.novelagent.provider-gemini",
    # Frozen Skill IDs.
    "com.plotpilot.skill.donor.atomic-breakdown",
    "com.plotpilot.skill.donor.claim-synthesis",
    "com.plotpilot.skill.donor.narrative-unit",
    "com.plotpilot.skill.donor.character-line",
    "com.plotpilot.skill.donor.foreshadow-line",
    "com.plotpilot.skill.donor.style-analysis",
    "com.plotpilot.skill.outline.book",
    "com.plotpilot.skill.outline.volume",
    "com.plotpilot.skill.outline.chapter",
    "com.plotpilot.skill.outline.plot-unit",
    "com.plotpilot.skill.writing.combat",
    "com.plotpilot.skill.writing.psychology",
    "com.plotpilot.skill.writing.dialogue",
    "com.plotpilot.skill.writing.scenery",
    "com.plotpilot.skill.writing.hook",
    "com.plotpilot.skill.writing.pacing",
    "com.plotpilot.skill.writing.refine",
    "com.plotpilot.skill.quality.consistency",
    "com.plotpilot.skill.style.apply",
    # Installed Style Data identities.
    "com.plotpilot.novelagent.style-pack.core-default",
    "com.plotpilot.novelagent.lexicon.zh-core",
})


class StyleManufacturingWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool=False) -> None:
        self.code=code; self.retryable=retryable; super().__init__(message)


class TerminalContractError(StyleManufacturingWorkerError): pass


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass
class _RunContext:
    request_hash: str; binding_hash: str; checkpoint_ids: tuple[str,...]
    last_job_event_seq: int=0; last_checkpoint_seq: int=0; last_local_seq: int=0
    terminal_dispatched: bool=False
    cancelled: threading.Event | None = None


@dataclass
class _ActiveRun:
    worker_run_id: str; binding_hash: str; cancelled: threading.Event


class _Dispatcher:
    def __init__(self)->None: self.lock=threading.RLock(); self.active: _ActiveRun|None=None
    def begin(self, worker_run_id:str,binding_hash:str)->_ActiveRun:
        with self.lock:
            if self.active is not None: raise StyleManufacturingWorkerError("WORKER_BUSY","style manufacturing already has an active operation",retryable=True)
            self.active=_ActiveRun(worker_run_id,binding_hash,threading.Event()); return self.active
    def cancel(self,worker_run_id:str,binding_hash:str)->bool:
        with self.lock:
            if self.active is None or self.active.worker_run_id!=worker_run_id or self.active.binding_hash!=binding_hash:return False
            self.active.cancelled.set(); return True
    def finish(self,active:_ActiveRun)->None:
        with self.lock:
            if self.active is active:self.active=None


_DISPATCHER=_Dispatcher()


def _require_sdk()->None:
    if _SDK_IMPORT_ERROR is not None or any(x is None for x in (_validate_rpc_result_sdk,_verify_checkpoint_sdk,_verify_provenance_receipt_sdk,_verify_result_bundle_sdk)):
        raise StyleManufacturingWorkerError("SDK_UNAVAILABLE","public PlotPilot SDK is unavailable; fail-closed") from _SDK_IMPORT_ERROR


def _id(value:Any,label:str)->str:
    if not isinstance(value,str) or _ID_RE.fullmatch(value) is None: raise StyleManufacturingWorkerError("INPUT_INVALID",f"{label} must be a Core identifier")
    return value


def _hash(value:Any,label:str)->str:
    if not isinstance(value,str) or _HASH_RE.fullmatch(value) is None: raise StyleManufacturingWorkerError("INPUT_INVALID",f"{label} must be lowercase SHA-256")
    return value


def _integer(value:Any,label:str,minimum:int=0)->int:
    if isinstance(value,bool) or not isinstance(value,int) or value<minimum: raise StyleManufacturingWorkerError("INPUT_INVALID",f"{label} must be integer >= {minimum}")
    return value


def _derived_id(prefix:str,*parts:str)->str:
    return f"{prefix}:{hashlib.sha256((prefix+'\n'+'\n'.join(parts)).encode()).hexdigest()[:48]}"


def _host_call(host:HostPort|None,method:str,params:Mapping[str,object])->dict[str,object]:
    _require_sdk()
    if host is None or not hasattr(host,"call"): raise StyleManufacturingWorkerError("HOST_REQUIRED","Core HostPort.call is required")
    request=dict(params)
    try: response=host.call(method,request)
    except Exception as exc: raise StyleManufacturingWorkerError("HOST_RPC_ERROR",f"{method}: {type(exc).__name__}: {exc}",retryable=True) from exc
    if not isinstance(response,Mapping): raise StyleManufacturingWorkerError("HOST_CONTRACT_ERROR",f"{method} returned non-object")
    assert _validate_rpc_result_sdk is not None
    # Some accepted host adapters attach response provenance to
    # model.invoke.  Those fields are verified by ``_model_receipt_assertion``
    # below, while the public RPC verifier still validates the frozen
    # acknowledgement projection.  No unrelated extension is tolerated.
    validation_response=dict(response)
    if method=="host.model.invoke/v1":
        extension_fields={
            "content_hash", "model_receipt", "provenance", "model_profile_revision_id",
            "route_id", "attempt_id", "run_snapshot_hash", "request_asset_id",
            "request_asset_hash", "response_asset_id", "response_asset_hash",
        }
        advertised=set(validation_response)-{"state","response_asset_id","receipt_id","uncertainty"}
        if not advertised.issubset(extension_fields):
            raise StyleManufacturingWorkerError("HOST_CONTRACT_ERROR", "model.invoke returned an unknown extension")
        for key in ("content_hash", "request_asset_hash", "response_asset_hash", "run_snapshot_hash"):
            if key in validation_response:
                _hash(validation_response[key], f"model response {key}")
                validation_response.pop(key, None)
        # These are plugin-side provenance evidence, not part of the public
        # RPC acknowledgement schema.  They are consumed after the SDK
        # envelope has been checked.
        for key in extension_fields-{"response_asset_id"}:
            validation_response.pop(key, None)
    try: _validate_rpc_result_sdk(method,validation_response,request={"method":method,"params":request})
    except Exception as exc: raise StyleManufacturingWorkerError("HOST_CONTRACT_ERROR",f"{method} result failed public RPC schema: {exc}") from exc
    return dict(response)


def _read_asset(host:HostPort|None,asset_id:str,expected_hash:str|None)->bytes:
    _id(asset_id,"asset_id")
    if expected_hash is not None: _hash(expected_hash,"asset hash")
    offset=0; chunks=[]
    for _ in range(_MAX_PAGES):
        response=_host_call(host,"host.asset.read/v1",{"asset_id":asset_id,"offset":offset,"length":_PAGE_SIZE})
        encoded=response.get("base64_chunk")
        if not isinstance(encoded,str): raise StyleManufacturingWorkerError("ASSET_READ_ERROR","missing base64_chunk")
        try: chunk=base64.b64decode(encoded,validate=True)
        except Exception as exc: raise StyleManufacturingWorkerError("ASSET_READ_ERROR","invalid base64") from exc
        if response.get("content_hash")!=sha256_bytes(chunk) or len(chunk)>_PAGE_SIZE: raise StyleManufacturingWorkerError("ASSET_READ_ERROR","page hash/size mismatch")
        chunks.append(chunk); next_offset=response.get("next_offset")
        if next_offset is None: break
        if isinstance(next_offset,bool) or not isinstance(next_offset,int) or next_offset!=offset+len(chunk) or next_offset<=offset: raise StyleManufacturingWorkerError("ASSET_READ_ERROR","non-contiguous page")
        offset=next_offset
    else: raise StyleManufacturingWorkerError("ASSET_READ_ERROR","page limit exceeded")
    data=b"".join(chunks)
    if expected_hash is not None and sha256_bytes(data)!=expected_hash: raise StyleManufacturingWorkerError("ASSET_READ_ERROR","Asset hash mismatch")
    return data


def _strict_json(raw:bytes)->Any:
    def pairs(rows):
        result={}
        for k,v in rows:
            if k in result: raise ValueError("duplicate key")
            result[k]=v
        return result
    try:return json.loads(raw.decode("utf-8","strict"),object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in()).throw(ValueError(x)))
    except Exception as exc:raise StyleManufacturingWorkerError("ASSET_READ_ERROR","Asset is not strict UTF-8 JSON") from exc


def _load_json_asset(host:HostPort|None,asset_id:str,expected_hash:str)->Any:
    raw = _read_asset(host,asset_id,expected_hash)
    value = _strict_json(raw)
    # JSON Assets used as frozen model inputs are canonical bytes, not merely
    # semantically equivalent JSON.  This prevents a caller from substituting
    # a differently encoded object while retaining the same parsed value.
    try:
        if sha256_bytes(canonical_json(value)) != expected_hash:
            raise StyleManufacturingWorkerError("ASSET_READ_ERROR", "JSON Asset is not canonical")
    except TypeError as exc:
        raise StyleManufacturingWorkerError("ASSET_READ_ERROR", "JSON Asset cannot be canonicalized") from exc
    return value


def _sealed_case_asset(host:HostPort|None, request:Mapping[str,Any], ctx:_RunContext,
                       pack:Mapping[str,Any], rubric_raw:bytes) -> tuple[str,str,dict[str,Any]]:
    """Load and validate the exact qualification case set.

    A caller may provide either canonical ``sealed_case_asset_*`` fields or
    the historical plural aliases.  Legacy requests receive a deterministic
    case Asset generated from the exact style/rubric hashes; no model call is
    made without this materialized binding.
    """
    asset_id = request.get("sealed_case_asset_id") or request.get("sealed_cases_asset_id")
    asset_hash = request.get("sealed_case_asset_hash") or request.get("sealed_cases_asset_hash")
    if asset_id is None:
        value = {
            "schema": "style.qualify.sealed-case-asset/v1",
            "style_pack_id": pack["style_pack_id"],
            "style_release_id": pack["style_release_id"],
            "style_payload_hash": pack["payload_hash"],
            "rubric_asset_hash": sha256_bytes(rubric_raw),
            "cases": [
                {"case_id": f"blind-case-{index}",
                 "prompt": f"请以目标文风改写资格盲测片段 {index}，保留事实并避免复述范文。"}
                for index in range(1, 4)
            ],
        }
        data = canonical_json(value)
        asset_id = _upload(host, ctx, data, "application/json", "sealed-cases")
        asset_hash = sha256_bytes(data)
    raw = _load_json_asset(host, str(asset_id), _hash(asset_hash, "sealed_case_asset_hash"))
    if not isinstance(raw, Mapping) or set(raw) != {"schema", "style_pack_id", "style_release_id", "style_payload_hash", "rubric_asset_hash", "cases"}:
        raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case Asset is not closed")
    if (raw["schema"] != "style.qualify.sealed-case-asset/v1"
            or raw["style_pack_id"] != pack["style_pack_id"]
            or raw["style_release_id"] != pack["style_release_id"]
            or raw["style_payload_hash"] != pack["payload_hash"]
            or raw["rubric_asset_hash"] != sha256_bytes(rubric_raw)):
        raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case Asset is not bound to exact style/rubric")
    cases = raw["cases"]
    if not isinstance(cases, list) or len(cases) != 3:
        raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case Asset requires exactly three cases")
    ids: list[str] = []
    for case in cases:
        if not isinstance(case, Mapping) or set(case) != {"case_id", "prompt"}:
            raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case row is not closed")
        _id(case["case_id"], "sealed case_id")
        if not isinstance(case["prompt"], str) or len(case["prompt"].strip()) < 2:
            raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case prompt is empty")
        ids.append(case["case_id"])
    if ids != ["blind-case-1", "blind-case-2", "blind-case-3"] or len(set(ids)) != 3:
        raise StyleManufacturingWorkerError("CASE_ASSET_INVALID", "sealed case IDs are not the frozen blind set")
    return str(asset_id), str(asset_hash), dict(raw)


def _frozen_input_binding(asset_id: str, asset_hash: str, value: Any, label: str) -> dict[str, Any]:
    """Return a self-contained, hash-bound model input attachment.

    The model broker receives this projection as part of its immutable request
    Asset.  ``content`` is canonical JSON text rather than a mutable Python
    object so a broker cannot silently reinterpret key order/number encoding.
    """
    _id(asset_id, f"{label}.asset_id")
    _hash(asset_hash, f"{label}.asset_hash")
    raw = canonical_json(value)
    observed = sha256_bytes(raw)
    if observed != asset_hash:
        raise StyleManufacturingWorkerError("FROZEN_INPUT_INVALID", f"{label} Asset hash does not match exact content")
    return {
        "asset_id": asset_id,
        "asset_hash": asset_hash,
        "content_hash": observed,
        "content": raw.decode("utf-8"),
    }


def _frozen_model_inputs(*, style_pack: Mapping[str, Any], style_pack_asset_id: str,
                         style_pack_asset_hash: str, sealed_case: Mapping[str, Any],
                         sealed_case_asset_id: str, sealed_case_asset_hash: str,
                         rubric: Any, rubric_asset_id: str, rubric_asset_hash: str) -> dict[str, Any]:
    return {
        "style_pack": _frozen_input_binding(style_pack_asset_id, style_pack_asset_hash, style_pack, "style_pack"),
        "sealed_case": _frozen_input_binding(sealed_case_asset_id, sealed_case_asset_hash, sealed_case, "sealed_case"),
        "rubric": _frozen_input_binding(rubric_asset_id, rubric_asset_hash, rubric, "rubric"),
    }


def _writer_text_is_qualifying(
    text: str,
    *,
    case: Mapping[str, Any] | None = None,
    rubric: Mapping[str, Any] | None = None,
    style_pack: Mapping[str, Any] | None = None,
) -> bool:
    """Apply a deterministic, conservative gate to one frozen writer case.

    The model is not the authority for qualification: a reviewer score must
    never turn an empty/placeholder response into an eligible release.  The
    gate deliberately requires the exact sealed case, rubric, and style-pack
    context so a caller cannot evaluate a response outside the frozen binding.
    It uses token diversity, entropy, and repetition structure rather than a
    response denylist, and therefore remains deterministic across hosts.
    """
    if (not isinstance(text, str) or not isinstance(case, Mapping)
            or not isinstance(rubric, Mapping) or not isinstance(style_pack, Mapping)):
        return False
    prompt = case.get("prompt")
    if not isinstance(prompt, str) or len("".join(prompt.split())) < 2:
        return False

    compact = "".join(text.split())
    if len(compact) < 5:
        return False

    # Allow a rubric to raise the floor, but never permit malformed/non-integer
    # configuration to weaken the conservative default.
    configured_minimums = [
        rubric.get("minimum_output_chars"), rubric.get("min_output_chars"),
        rubric.get("minimum_characters"), rubric.get("min_chars"),
        case.get("minimum_output_chars"), case.get("min_output_chars"),
    ]
    minimum = 5
    for configured in configured_minimums:
        if isinstance(configured, int) and not isinstance(configured, bool) and configured >= 5:
            minimum = max(minimum, configured)
    if len(compact) < minimum:
        return False

    content = [char.casefold() for char in compact if char.isalnum()]
    if not content or not any(char.isalpha() for char in compact):
        # A string made solely of numbers/punctuation is not a writing sample.
        return False
    if len(set(content)) < 2:
        return False

    def is_cjk(char: str) -> bool:
        return "\u3400" <= char <= "\u9fff"

    def quality_tokens(value: str) -> list[str]:
        tokens: list[str] = []
        for raw in _QUALITY_TOKEN_RE.findall(value):
            folded = raw.casefold()
            if raw and all(is_cjk(char) for char in raw):
                tokens.extend(char.casefold() for char in raw)
            else:
                tokens.append(folded)
        return tokens

    def entropy(values: list[str]) -> float:
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        size = len(values)
        return -sum(
            (count / size) * math.log2(count / size)
            for count in counts.values()
        )

    def is_periodic(values: list[str]) -> bool:
        size = len(values)
        if size < 6:
            return False
        for period in range(1, size // 2 + 1):
            if size % period == 0 and values == values[:period] * (size // period):
                return True
        return False

    tokens = quality_tokens(text)
    if not tokens:
        return False
    token_counts: dict[str, int] = {}
    for token in tokens:
        token_counts[token] = token_counts.get(token, 0) + 1

    # Placeholder words are treated as a structural class.  A marker only
    # matters when it dominates the token stream; ordinary prose may mention
    # a word such as ``test`` without becoming a placeholder response.
    placeholder_count = sum(
        count for token, count in token_counts.items()
        if token in _QUALITY_PLACEHOLDER_TOKENS
    )
    if placeholder_count and placeholder_count * 2 >= len(tokens):
        return False

    unique_tokens = len(token_counts)
    most_common = max(token_counts.values())
    if unique_tokens * 2 <= len(tokens) or most_common >= 3:
        return False
    # Entropy catches low-information alternating token streams even when no
    # one token is repeated three times in a row.
    if len(tokens) >= 3 and entropy(tokens) <= 1.0:
        return False
    # The same test applies to characters, making periodic and padded text
    # language-independent without comparing against whole response strings.
    if len(content) >= 6 and (entropy(content) <= 1.0 or is_periodic(content)):
        return False

    cjk_count = sum(is_cjk(char) for char in compact)
    cjk_hint = any(is_cjk(char) for char in prompt)
    features = style_pack.get("features")
    if isinstance(features, Mapping):
        cjk_hint = cjk_hint or any(
            isinstance(item, str) and any(is_cjk(char) for char in item)
            for rows in features.values() if isinstance(rows, list)
            for item in rows
        )

    # Short CJK samples can be meaningful at four distinct characters.  For
    # Latin-only samples require real token structure (or sentence
    # punctuation) and a longer floor; this rejects arbitrary five-letter
    # placeholders such as ``abcde`` without a special-case comparison.
    if cjk_count:
        if cjk_count < 3 and len(compact) < 8:
            return False
    else:
        sentence_punctuation = any(char in text for char in ".!?;:,，。！？；：")
        if not sentence_punctuation and (len(compact) < 12 or len(tokens) < 2):
            return False
        if sentence_punctuation and len(compact) < 8:
            return False
    # A Chinese rubric/case should not be satisfied by a tiny ASCII token;
    # require at least one CJK scalar for such short responses.
    if cjk_hint and len(compact) < 8 and not cjk_count:
        return False
    return True


def _model_binding_assertion(output: Mapping[str, Any], *, profile: str, route: str,
                             attempt_ids: list[str], snapshot_hash: str) -> None:
    """Check optional broker-signed model provenance when present.

    Older HostPort fixtures expose only the closed response envelope.  New
    hosts may include a ``provenance`` object (or its flattened fields); when
    present every value is required to agree with the frozen RunSnapshot
    request so a foreign profile/route/attempt cannot become eligible.
    """
    has_nested = isinstance(output, Mapping) and "provenance" in output
    raw = output.get("provenance") if has_nested else None
    if raw is None and not has_nested:
        raw = {key: output[key] for key in ("model_profile_revision_id", "route_id", "attempt_id", "run_snapshot_hash") if key in output}
    # A legacy response with no provenance extension remains accepted for the
    # frozen HostPort contract; once a provenance field is advertised it must
    # be a complete non-empty binding rather than an empty bypass object.
    if not raw:
        if has_nested:
            raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model provenance is empty")
        return
    if not isinstance(raw, Mapping):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model provenance is not an object")
    if raw.get("model_profile_revision_id") != profile or raw.get("route_id") != route or raw.get("run_snapshot_hash") != snapshot_hash:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model profile/route/snapshot drifted")
    if "attempt_id" in raw and raw["attempt_id"] not in set(attempt_ids):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model attempt is outside the frozen attempt path")


def _model_receipt_assertion(response: Mapping[str, Any], *, receipt_id: str,
                             profile: str, route: str, attempt_ids: list[str],
                             snapshot_hash: str, request_asset_id: str,
                             request_asset_hash: str, response_asset_id: str,
                             response_asset_hash: str) -> None:
    """Verify Core model-receipt provenance before consuming model output.

    The public ``host.model.invoke/v1`` acknowledgement intentionally has a
    small closed shape.  A Core adapter must attach a complete broker receipt
    either as ``model_receipt`` or ``provenance``.  A receipt-shaped identifier
    by itself is never qualification evidence: missing, foreign, or forged
    provenance is rejected before the model output is consumed.
    """
    if "model_receipt" in response and "provenance" in response and response["model_receipt"] != response["provenance"]:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt provenance aliases disagree")
    extension = response.get("model_receipt", response.get("provenance"))
    # A Core adapter may flatten the same receipt projection on the RPC
    # acknowledgement.  Treat the presence of any provenance field as an
    # assertion, rather than silently falling back to the legacy fixture path.
    if extension is None and any(key in response for key in (
        "model_profile_revision_id", "route_id", "attempt_id", "run_snapshot_hash",
        "request_asset_id", "request_asset_hash", "response_asset_hash",
    )):
        extension = {key: response[key] for key in (
            "receipt_id", "model_profile_revision_id", "route_id", "attempt_id",
            "run_snapshot_hash", "request_asset_id", "request_asset_hash",
            "response_asset_id", "response_asset_hash",
        ) if key in response}
    if extension is None:
        # A receipt-shaped identifier is not provenance.  Qualification must
        # consume a Host/Core-issued receipt projection that can be checked
        # against the exact invocation and its immutable Assets.
        raise StyleManufacturingWorkerError(
            "MODEL_PROVENANCE_INVALID",
            "model receipt lacks verifiable Host/Core provenance",
        )
    if not isinstance(extension, Mapping):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt provenance is not an object")
    required = {
        "receipt_id", "model_profile_revision_id", "route_id", "attempt_id",
        "run_snapshot_hash", "request_asset_id", "request_asset_hash",
        "response_asset_id", "response_asset_hash",
    }
    if not required.issubset(set(extension)):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt provenance is incomplete")
    if extension.get("receipt_id") != receipt_id:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt ID drifted")
    if extension.get("model_profile_revision_id") != profile or extension.get("route_id") != route:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt profile/route drifted")
    if extension.get("run_snapshot_hash") != snapshot_hash:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt snapshot drifted")
    if extension.get("attempt_id") not in set(attempt_ids):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt attempt is outside the frozen attempt path")
    if extension.get("request_asset_id") != request_asset_id or extension.get("response_asset_id") != response_asset_id:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt Asset identity drifted")
    if extension.get("request_asset_hash") != request_asset_hash or extension.get("response_asset_hash") != response_asset_hash:
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model receipt Asset hash drifted")
    for field in ("request_asset_hash", "response_asset_hash"):
        _hash(extension[field], f"model receipt {field}")


_MODEL_PROVENANCE_FIELDS = frozenset({"provenance", "model_profile_revision_id", "route_id", "attempt_id", "run_snapshot_hash"})


def _without_model_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in _MODEL_PROVENANCE_FIELDS}


def _chunk_plan_asset(host:HostPort|None, request:Mapping[str,Any], ctx:_RunContext, source:bytes) -> tuple[str,str,dict[str,Any]]:
    """Materialize and verify one immutable chunk plan for manufacture."""
    supplied_id = request.get("chunk_plan_asset_id")
    supplied_hash = request.get("chunk_plan_asset_hash")
    if supplied_id is None:
        # Split on UTF-8 byte boundaries only at valid code-point boundaries so
        # each map receives reproducible text and a content hash.
        text = source.decode("utf-8", "strict")
        count = max(1, int(request["total_units"]))
        # Use proportional character boundaries so exactly ``count`` chunks
        # are materialized, including deterministic empty trailing chunks when
        # the requested unit count exceeds source length.  Empty chunks retain
        # the contiguous end offset and are still represented as map units.
        spans = []
        for index in range(count):
            start = (index * len(text)) // count
            end = ((index + 1) * len(text)) // count
            chunk_text = text[start:end]
            raw = chunk_text.encode("utf-8")
            spans.append((len(text[:start].encode("utf-8")), len(raw), chunk_text))
        plan = {
            "schema": "style.chunk-plan/v1",
            "style_pack_id": request["style_pack_id"],
            "source_asset_id": request["source_asset_id"],
            "source_asset_hash": request["source_asset_hash"],
            "source_revision_id": request["source_revision_id"],
            "chunk_plan_hash": request["chunk_plan_hash"],
            "chunks": [
                {"chunk_id": f"chunk-{index + 1}", "ordinal": index,
                 "offset": offset, "length": length,
                 "content_hash": sha256_bytes(text_value.encode("utf-8")),
                 "text": text_value}
                for index, (offset, length, text_value) in enumerate(spans[:count])
            ],
        }
        raw_plan = canonical_json(plan)
        supplied_id = _upload(host, ctx, raw_plan, "application/json", "chunk-plan")
        supplied_hash = sha256_bytes(raw_plan)
    plan = _load_json_asset(host, str(supplied_id), _hash(supplied_hash, "chunk_plan_asset_hash"))
    if not isinstance(plan, Mapping) or set(plan) != {"schema", "style_pack_id", "source_asset_id", "source_asset_hash", "source_revision_id", "chunk_plan_hash", "chunks"}:
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk plan Asset is not closed")
    if (plan["schema"] != "style.chunk-plan/v1" or plan["style_pack_id"] != request["style_pack_id"]
            or plan["source_asset_id"] != request["source_asset_id"]
            or plan["source_asset_hash"] != request["source_asset_hash"]
            or plan["source_revision_id"] != request["source_revision_id"]
            or plan["chunk_plan_hash"] != request["chunk_plan_hash"]):
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk plan is not bound to exact source/request")
    chunks = plan["chunks"]
    if not isinstance(chunks, list) or len(chunks) != max(1, int(request["total_units"])):
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk plan cardinality differs from total_units")
    observed: list[int] = []
    observed_ids: list[str] = []
    observed_end = 0
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, Mapping) or set(chunk) != {"chunk_id", "ordinal", "offset", "length", "content_hash", "text"}:
            raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk row is not closed")
        _id(chunk["chunk_id"], "chunk_id")
        if chunk["ordinal"] != index or isinstance(chunk["offset"], bool) or not isinstance(chunk["offset"], int) or chunk["offset"] < 0 or isinstance(chunk["length"], bool) or not isinstance(chunk["length"], int) or chunk["length"] < 0:
            raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk ordering/range invalid")
        _hash(chunk["content_hash"], "chunk content_hash")
        if not isinstance(chunk["text"], str) or sha256_bytes(chunk["text"].encode("utf-8")) != chunk["content_hash"]:
            raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk content hash mismatch")
        if len(chunk["text"].encode("utf-8")) != chunk["length"]:
            raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk byte length mismatch")
        # The plan is a materialized projection of the exact source Asset,
        # not merely an ordered list of labels.  Enforce contiguous byte
        # ranges and retain the source reconstruction for the final check.
        if chunk["offset"] != observed_end:
            raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk ranges are not contiguous")
        observed_end = chunk["offset"] + chunk["length"]
        observed.append(chunk["offset"])
        observed_ids.append(chunk["chunk_id"])
    if observed != sorted(observed):
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk offsets are not ordered")
    if len(observed_ids) != len(set(observed_ids)):
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk IDs are not unique")
    if observed_end != len(source) or b"".join(str(chunk["text"]).encode("utf-8") for chunk in chunks) != source:
        raise StyleManufacturingWorkerError("CHUNK_PLAN_INVALID", "chunk plan does not reconstruct the exact source Asset")
    return str(supplied_id), str(supplied_hash), dict(plan)


def _upload(host:HostPort|None,ctx:_RunContext,data:bytes,mime:str,suffix:str,*,operation_key:str|None=None,upload_id:str|None=None)->str:
    expected=sha256_bytes(data); op=operation_key or ctx.request_hash; uid=upload_id or _derived_id("upload",ctx.request_hash,suffix)
    chunks=[b""] if not data else [data[i:i+_PAGE_SIZE] for i in range(0,len(data),_PAGE_SIZE)]; accepted=0; asset_id=None
    for index,chunk in enumerate(chunks):
        final=index==len(chunks)-1
        response=_host_call(host,"host.asset.create/v1",{"operation_key":op,"upload_id":uid,"offset":accepted,"mime":mime,"total_size":len(data),
            "expected_hash":expected,"chunk_hash":sha256_bytes(chunk),"base64_chunk":base64.b64encode(chunk).decode(),"final":final})
        if response.get("upload_id")!=uid or response.get("accepted_bytes")!=accepted+len(chunk) or response.get("completed") is not final: raise StyleManufacturingWorkerError("ASSET_CREATE_ERROR","upload acknowledgement mismatch")
        if final: asset_id=_id(response.get("asset_id"),"asset_id")
        accepted+=len(chunk)
    status=_host_call(host,"host.asset.upload.status/v1",{"upload_id":uid,"expected_hash":expected})
    if status.get("accepted_bytes")!=len(data) or status.get("completed") is not True or status.get("asset_id")!=asset_id: raise StyleManufacturingWorkerError("ASSET_UPLOAD_ERROR","upload status mismatch")
    assert asset_id is not None; return asset_id


def _binding_projection(request:Mapping[str,Any])->dict[str,Any]:
    value={k:deepcopy(v) for k,v in request.items() if k not in _RESUME}; value["operation"]="run"; return value


def _context(value:Mapping[str,Any],capability:str)->tuple[dict[str,Any],_RunContext]:
    if not isinstance(value,Mapping): raise StyleManufacturingWorkerError("INPUT_INVALID","request must be an object")
    request=dict(value); spec=SPEC_BY_CAPABILITY[capability]; op=request.get("operation")
    if op not in spec.supports: raise StyleManufacturingWorkerError("INPUT_INVALID",f"operation {op!r} is not declared")
    group={CAPABILITY_MANUFACTURE:_MANUFACTURE,CAPABILITY_QUALIFY:_QUALIFY,CAPABILITY_PACKAGE:_PACKAGE}[capability]
    optional = _MANUFACTURE_OPTIONAL if capability == CAPABILITY_MANUFACTURE else _QUALIFY_OPTIONAL if capability == CAPABILITY_QUALIFY else frozenset()
    required=set(_COMMON|group)
    if op=="resume":required.update(_RESUME)
    missing = required - set(request)
    extra = set(request) - required - set(optional)
    if missing or extra:raise StyleManufacturingWorkerError("INPUT_INVALID",f"request fields are not closed: missing={sorted(missing)}, extra={sorted(extra)}")
    if request["schema"]!=spec.input_schema or request["capability_id"]!=capability or request["operation_key"]!=capability:raise StyleManufacturingWorkerError("INPUT_INVALID","schema/capability/operation_key mismatch")
    for field in ("job_id","step_id","attempt_id","worker_run_id","provenance_receipt_id","workspace_id"):_id(request[field],field)
    _integer(request["lease_epoch"],"lease_epoch",1);_integer(request["total_units"],"total_units",1);_hash(request["run_snapshot_hash"],"run_snapshot_hash")
    if not isinstance(request["created_at"],str) or _TIME_RE.fullmatch(request["created_at"]) is None:raise StyleManufacturingWorkerError("INPUT_INVALID","created_at invalid")
    checkpoints=request["checkpoint_ids"]
    if not isinstance(checkpoints,list) or not checkpoints or len(checkpoints)!=len(set(checkpoints)):raise StyleManufacturingWorkerError("INPUT_INVALID","checkpoint_ids invalid")
    for x in checkpoints:_id(x,"checkpoint_id")
    if capability==CAPABILITY_MANUFACTURE:
        for f in ("style_pack_id","source_asset_id","source_revision_id","target_entity_id","target_base_revision_id","model_profile_revision_id","route_id"):_id(request[f],f)
        for f in ("source_asset_hash","target_base_content_hash","prompt_hash","output_schema_hash","chunk_plan_hash"):_hash(request[f],f)
        if not isinstance(request["style_version"],str) or SEMVER_RE.fullmatch(request["style_version"]) is None:raise StyleManufacturingWorkerError("INPUT_INVALID","style_version must be SemVer")
        if not isinstance(request["lexicon_asset_refs"],list):raise StyleManufacturingWorkerError("INPUT_INVALID","lexicon_asset_refs invalid")
        for row in request["lexicon_asset_refs"]:
            if not isinstance(row,Mapping) or set(row)!={"asset_id","asset_hash","data_plugin_id","data_release_id","bundle_hash","order"}:raise StyleManufacturingWorkerError("INPUT_INVALID","lexicon ref invalid")
            for f in ("asset_id","data_plugin_id"):_id(row[f],f)
            for f in ("asset_hash","data_release_id","bundle_hash"):_hash(row[f],f)
        _attempt_list(request["provider_attempt_ids"],"provider_attempt_ids")
        if "chunk_plan_asset_id" in request or "chunk_plan_asset_hash" in request:
            if not {"chunk_plan_asset_id", "chunk_plan_asset_hash"}.issubset(request):
                raise StyleManufacturingWorkerError("INPUT_INVALID", "chunk plan Asset binding is incomplete")
            _id(request["chunk_plan_asset_id"], "chunk_plan_asset_id"); _hash(request["chunk_plan_asset_hash"], "chunk_plan_asset_hash")
        for field in ("map_model_profile_revision_id", "synthesis_model_profile_revision_id"):
            if field in request: _id(request[field], field)
        for field in ("map_attempt_ids", "synthesis_attempt_ids"):
            if field in request: _attempt_list(request[field], field)
        if "map_attempt_ids" in request and "synthesis_attempt_ids" in request:
            if set(request["map_attempt_ids"]) & set(request["synthesis_attempt_ids"]):
                raise StyleManufacturingWorkerError("INPUT_INVALID", "map and synthesis attempt paths overlap")
    elif capability==CAPABILITY_QUALIFY:
        for f in ("style_pack_asset_id","rubric_asset_id","manufacturing_route_id","writer_route_id","reviewer_route_id","writer_model_profile_revision_id","reviewer_model_profile_revision_id"):_id(request[f],f)
        for f in ("style_pack_asset_hash","rubric_asset_hash"):_hash(request[f],f)
        if len({request["manufacturing_route_id"],request["writer_route_id"],request["reviewer_route_id"]})!=3:raise StyleManufacturingWorkerError("QUALIFICATION_PATH_INVALID","same-path auto-pass is forbidden")
        if request["writer_model_profile_revision_id"] == request["reviewer_model_profile_revision_id"]:
            raise StyleManufacturingWorkerError("QUALIFICATION_PATH_INVALID", "writer and reviewer model profiles must be distinct")
        sets=[set(_attempt_list(request[f],f)) for f in ("manufacturing_attempt_ids","writer_attempt_ids","reviewer_attempt_ids")]
        if any(sets[i]&sets[j] for i in range(3) for j in range(i+1,3)):raise StyleManufacturingWorkerError("QUALIFICATION_PATH_INVALID","attempt paths overlap")
    else:
        for f in ("style_pack_asset_id","qualification_receipt_asset_id","data_plugin_id"):_id(request[f],f)
        for f in ("style_pack_asset_hash","qualification_receipt_asset_hash"):_hash(request[f],f)
        if not isinstance(request["data_version"], str) or SEMVER_RE.fullmatch(request["data_version"]) is None:
            raise StyleManufacturingWorkerError("IDENTITY_DOMAIN_ERROR", "Data version must be SemVer")
        if request["data_plugin_id"] in _RESERVED_DATA_IDS:
            raise StyleManufacturingWorkerError("IDENTITY_DOMAIN_ERROR", "Data identity collides with a Code, Skill, or installed Data identity")
    if capability == CAPABILITY_QUALIFY:
        present = [name for name in ("sealed_case_asset_id", "sealed_cases_asset_id") if name in request]
        hashes = [name for name in ("sealed_case_asset_hash", "sealed_cases_asset_hash") if name in request]
        if present or hashes:
            if len(present) != 1 or len(hashes) != 1:
                raise StyleManufacturingWorkerError("INPUT_INVALID", "sealed case Asset aliases are ambiguous or incomplete")
            _id(request[present[0]], present[0]); _hash(request[hashes[0]], hashes[0])
    if op=="resume":
        for f in ("resume_checkpoint_asset_id","resume_state_asset_id"):_id(request[f],f)
        for f in ("resume_checkpoint_asset_hash","resume_state_asset_hash"):_hash(request[f],f)
    binding=hash_json(capability+"-binding/v1",_binding_projection(request)); request_hash=hash_json(capability+"-request/v1",request)
    return request,_RunContext(request_hash,binding,tuple(checkpoints))


def _attempt_list(value:Any,label:str)->list[str]:
    if not isinstance(value,list) or not value or len(value)!=len(set(value)):raise StyleManufacturingWorkerError("INPUT_INVALID",f"{label} must be non-empty unique")
    for x in value:_id(x,label)
    return list(value)


def _producer(request:Mapping[str,Any],capability:str,release_id:str)->dict[str,Any]:
    return {"plugin_id":PLUGIN_ID,"release_id":release_id,"capability_id":capability,"job_id":request["job_id"],"step_id":request["step_id"],"attempt_id":request["attempt_id"],"lease_epoch":request["lease_epoch"]}


def _bundle(request:Mapping[str,Any],capability:str,items:list[dict[str,Any]],release_id:str,*,failure:bool=False)->dict[str,Any]:
    spec=SPEC_BY_CAPABILITY[capability]; contract,btype=("diagnostic-bundle/v1","diagnostic") if failure else (spec.result_contract,spec.bundle_type)
    logical=hash_json(capability+"-request/v1",_binding_projection(request))
    bundle={"schema":"result-bundle/v1","contract_id":contract,"bundle_id":_derived_id("bundle",logical,contract),"bundle_type":btype,
        "producer":_producer(request,capability,release_id),"input_snapshot_hash":request["run_snapshot_hash"],"items":items,"warnings":[],
        "partial":failure,"provenance_receipt_id":request["provenance_receipt_id"],"skill_chain_result_refs":[]}
    _require_sdk();assert _verify_result_bundle_sdk is not None
    try:_verify_result_bundle_sdk(bundle,snapshot_workspace_id=request["workspace_id"] if contract=="candidate-batch/v1" else None,snapshot_hash_value=request["run_snapshot_hash"],attempt_state="failed" if failure else None)
    except Exception as exc:raise StyleManufacturingWorkerError("RESULT_CONTRACT_ERROR",str(exc)) from exc
    return bundle


def _event(host:HostPort|None,ctx:_RunContext,event_type:str,payload_asset_id:str|None,local_seq:int)->None:
    response=_host_call(host,"host.job.event/v1",{"operation_key":ctx.request_hash,"event_type":event_type,"payload_asset_id":payload_asset_id,"local_seq":local_seq})
    if response.get("accepted") is not True or not isinstance(response.get("job_event_seq"),int) or response["job_event_seq"]<=ctx.last_job_event_seq:raise StyleManufacturingWorkerError("HOST_CONTRACT_ERROR","event acknowledgement invalid")
    ctx.last_job_event_seq=int(response["job_event_seq"]);ctx.last_local_seq=local_seq


def _complete(host:HostPort|None,request:Mapping[str,Any],ctx:_RunContext,outcome:str,bundle_asset_id:str|None,stage_key:str|None,detail_asset_id:str|None,local_seq:int)->None:
    if ctx.terminal_dispatched:raise TerminalContractError("TERMINAL_ALREADY_DISPATCHED","terminal completion duplicated")
    ctx.terminal_dispatched=True
    response=_host_call(host,"host.job.complete/v1",{"operation_key":ctx.request_hash,"worker_run_id":request["worker_run_id"],"outcome":outcome,
        "result_bundle_asset_id":bundle_asset_id,"candidate_stage_operation_key":stage_key,"terminal_detail_asset_id":detail_asset_id,"local_seq":local_seq})
    expected={"succeeded":"succeeded","failed":"failed","cancelled":"cancelled"}[outcome]
    if response.get("attempt_state")!=expected or response.get("provenance_receipt_id")!=request["provenance_receipt_id"]:raise TerminalContractError("TERMINAL_CONTRACT_ERROR","terminal acknowledgement mismatch")


def _invoke_model_detailed(host:HostPort|None,request:Mapping[str,Any],ctx:_RunContext,profile:str,suffix:str,payload:Mapping[str,Any])->tuple[dict[str,Any],str,str,str]:
    data=canonical_json(payload); request_asset_hash=sha256_bytes(data); request_asset_id=_upload(host,ctx,data,"application/json",f"model-request-{suffix}")
    response=_host_call(host,"host.model.invoke/v1",{"operation_key":_derived_id("model-operation",ctx.binding_hash,suffix),
        "invocation_id":_derived_id("invocation",ctx.binding_hash,suffix),"invocation_key":_derived_id("model-key",ctx.binding_hash,suffix),
        "model_profile_revision_id":profile,"request_asset_id":request_asset_id,"replay_policy":"manual_if_unknown"})
    if response.get("state")!="received" or response.get("uncertainty") is not None:raise StyleManufacturingWorkerError("MODEL_INVOKE_ERROR","model response uncertain",retryable=True)
    receipt_id=_id(response.get("receipt_id"),"model receipt_id"); output_id=_id(response.get("response_asset_id"),"response_asset_id")
    content_hash=response.get("content_hash")
    output_raw = _read_asset(host,output_id,_hash(content_hash,"content_hash") if content_hash is not None else None)
    output_hash = sha256_bytes(output_raw)
    expected_route = str(payload.get("route_id", request.get("route_id", "")))
    expected_snapshot = str(payload.get("run_snapshot_hash", request["run_snapshot_hash"]))
    expected_attempts = payload.get("attempt_ids")
    if not isinstance(expected_attempts, list):
        expected_attempts = request.get("provider_attempt_ids")
    if not isinstance(expected_attempts, list):
        expected_attempts = request.get("writer_attempt_ids") if suffix == "qualification-writer" else request.get("reviewer_attempt_ids")
    if not isinstance(expected_attempts, list):
        raise StyleManufacturingWorkerError("MODEL_PROVENANCE_INVALID", "model attempt path is missing")
    _model_receipt_assertion(response, receipt_id=receipt_id, profile=profile,
                             route=expected_route, attempt_ids=list(expected_attempts),
                             snapshot_hash=expected_snapshot, request_asset_id=request_asset_id,
                             request_asset_hash=request_asset_hash, response_asset_id=output_id,
                             response_asset_hash=output_hash)
    output=_strict_json(output_raw)
    if not isinstance(output,dict):raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","model output must be an object")
    # The broker response Asset is immutable.  Re-publish a canonical bound
    # copy as a plugin-owned Asset so downstream qualification can prove that
    # it reviewed exactly these bytes, even when a legacy broker omits the
    # optional content_hash acknowledgement.
    bound_data=canonical_json(output)
    bound_id=_upload(host,ctx,bound_data,"application/json",f"model-output-{suffix}")
    return output,receipt_id,bound_id,sha256_bytes(bound_data)


def _invoke_model(host:HostPort|None,request:Mapping[str,Any],ctx:_RunContext,profile:str,suffix:str,payload:Mapping[str,Any])->tuple[dict[str,Any],str]:
    output, receipt_id, _asset_id, _asset_hash = _invoke_model_detailed(host, request, ctx, profile, suffix, payload)
    return output, receipt_id


def _candidate(request:Mapping[str,Any],pack:Mapping[str,Any],asset_id:str)->dict[str,Any]:
    payload_hash=sha256_bytes(canonical_json(pack)); item_id=_derived_id("candidate",request["run_snapshot_hash"],pack["style_release_id"])
    return {"schema":"candidate-item/v1","item_id":item_id,"item_kind":"relation_set",
        "target":{"workspace_id":request["workspace_id"],"entity_kind":"relation_set","entity_id":request["target_entity_id"]},
        "mutation":{"mode":"relation_patch","payload_schema":"style-pack/v1","payload_hash":payload_hash},"payload_asset_id":asset_id,
        "base":{"revision_id":request["target_base_revision_id"],"content_hash":request["target_base_content_hash"]},
        "write_set":[{"workspace_id":request["workspace_id"],"entity_kind":"relation_set","entity_id":request["target_entity_id"],
            "revision_id":request["target_base_revision_id"],"content_hash":request["target_base_content_hash"]}],"parent_candidate_ids":[],
        "source_refs":[{"workspace_id":request["workspace_id"],"source_type":"canonical_revision","source_id":request["source_asset_id"],"revision_or_hash":request["source_revision_id"]}],"status":"complete"}


def _diagnostic(request:Mapping[str,Any],ctx:_RunContext,host:HostPort|None,details:Mapping[str,Any],code:str,message:str,*,status:str="complete",severity:str="info")->dict[str,Any]:
    data=canonical_json(details); asset_id=_upload(host,ctx,data,"application/json","diagnostic-"+code.lower())
    return {"schema":"diagnostic-item/v1","item_id":_derived_id("diagnostic",ctx.binding_hash,code),"severity":severity,"code":code,"message":message,
        "details_asset_id":asset_id,"details_hash":sha256_bytes(data),"source_refs":[],"status":status}


def _artifact(request:Mapping[str,Any],payload:Mapping[str,Any],asset_id:str)->dict[str,Any]:
    return {"schema":"artifact-item/v1","item_id":_derived_id("artifact",request["run_snapshot_hash"],sha256_bytes(canonical_json(payload))),
        "artifact_kind":"style-data-package/v1","payload_asset_id":asset_id,"payload_hash":sha256_bytes(canonical_json(payload)),
        "mime":"application/json","source_refs":[{"workspace_id":request["workspace_id"],"source_type":"style_release","source_id":payload["style_pack"]["style_pack_id"],"revision_or_hash":payload["style_pack"]["style_release_id"]}],"status":"complete"}


def _receipt(request:Mapping[str,Any],capability:str,package_hash:str,release_id:str,bundle:Mapping[str,Any]|None,model_receipts:list[str],staged:list[str])->dict[str,Any]:
    value={"schema":"provenance-receipt/v1","receipt_id":request["provenance_receipt_id"],"plugin_id":PLUGIN_ID,"release_id":release_id,"package_hash":package_hash,
        "capability_id":capability,"job_id":request["job_id"],"step_id":request["step_id"],"attempt_id":request["attempt_id"],"lease_epoch":request["lease_epoch"],
        "run_snapshot_hash":request["run_snapshot_hash"],"bundle_id":None if bundle is None else bundle["bundle_id"],"bundle_hash":None if bundle is None else hash_json("result-bundle/v1",bundle),
        "parent_receipt_ids":[],"model_receipt_ids":model_receipts,"skill_chain_result_refs":[],"staged_items":staged,"created_at":request["created_at"]}
    value["receipt_hash"]=hash_json("provenance-receipt/v1",value)
    assert _verify_provenance_receipt_sdk is not None
    try:_verify_provenance_receipt_sdk(value)
    except Exception as exc:raise StyleManufacturingWorkerError("RECEIPT_CONTRACT_ERROR",str(exc)) from exc
    return value


def _checkpoint(host:HostPort|None,request:Mapping[str,Any],ctx:_RunContext,state:Mapping[str,Any])->dict[str,Any]:
    checkpoint_seq = ctx.last_checkpoint_seq + 1
    state_data=canonical_json(state);state_id=_upload(host,ctx,state_data,"application/json",f"resume-state-{checkpoint_seq}")
    checkpoint_id = (ctx.checkpoint_ids[ctx.last_checkpoint_seq]
                     if ctx.last_checkpoint_seq < len(ctx.checkpoint_ids)
                     else _derived_id("checkpoint", ctx.binding_hash, str(ctx.last_checkpoint_seq + 1)))
    completed_units = state.get("completed_maps", request["total_units"])
    if isinstance(completed_units, bool) or not isinstance(completed_units, int) or not 0 <= completed_units <= request["total_units"]:
        raise StyleManufacturingWorkerError("CHECKPOINT_ERROR", "completed unit count is invalid")
    cp={"schema":"checkpoint/v1","checkpoint_id":checkpoint_id,"checkpoint_seq":ctx.last_checkpoint_seq+1,
        "job_id":request["job_id"],"step_id":request["step_id"],"source_attempt_id":request["attempt_id"],"lease_epoch":request["lease_epoch"],
        "run_snapshot_hash":request["run_snapshot_hash"],"replay_policy":"checkpoint_resume","completed_units":completed_units,"total_units":request["total_units"],
        "unit_set_hash":sha256_bytes(state_data),"state_asset_id":state_id,"created_at":request["created_at"]}
    cp["checkpoint_hash"]=hash_json("checkpoint/v1",cp);assert _verify_checkpoint_sdk is not None;_verify_checkpoint_sdk(cp,expected_snapshot_hash=request["run_snapshot_hash"])
    cp_data=canonical_json(cp);cp_asset=_upload(host,ctx,cp_data,"application/json",f"checkpoint-{checkpoint_seq}")
    response=_host_call(host,"host.checkpoint.commit/v1",{"operation_key":ctx.request_hash+"-checkpoint","checkpoint_asset_id":cp_asset})
    if response.get("checkpoint_id")!=cp["checkpoint_id"]:raise StyleManufacturingWorkerError("CHECKPOINT_ERROR","checkpoint acknowledgement mismatch")
    ctx.last_checkpoint_seq+=1
    return {"checkpoint":cp,"checkpoint_asset_id":cp_asset,"checkpoint_asset_hash":sha256_bytes(cp_data),"state_asset_id":state_id,"state_asset_hash":sha256_bytes(state_data)}


class StyleManufacturingPlugin:
    def __init__(self)->None:
        identity=load_runtime_identity();self.package_hash=str(identity["package_hash"]);self.release_id=str(identity["release_id"])
        self.last_receipt:dict[str,Any]|None=None;self.last_checkpoint:dict[str,Any]|None=None;self.last_qualification_receipt:dict[str,Any]|None=None
        self.last_manufacture_state:dict[str,Any]|None=None

    def cancel(self,request:Mapping[str,Any])->None:
        capability=str(request.get("capability_id"));_,ctx=_context(request,capability)
        if not _DISPATCHER.cancel(request["worker_run_id"],ctx.binding_hash):raise StyleManufacturingWorkerError("CANCEL_NOT_ACTIVE","no matching active run")
        return None

    def manufacture(self,request:Mapping[str,Any],host:HostPort|None,ctx:_RunContext)->tuple[dict[str,Any],list[str]]:
        source=_read_asset(host,request["source_asset_id"],request["source_asset_hash"])
        if int(request["total_units"]) > 1 or "chunk_plan_asset_id" in request:
            return self._manufacture_mapped(request, host, ctx, source)
        lexicons=[]
        for ref in request["lexicon_asset_refs"]:
            lexicons.append({"data_plugin_id":ref["data_plugin_id"],"data_release_id":ref["data_release_id"],"bundle_hash":ref["bundle_hash"],"order":ref["order"],
                "asset_hash":sha256_bytes(_read_asset(host,ref["asset_id"],ref["asset_hash"]))})
        model_request={"schema":"style.manufacture-model-request/v1","source_asset_id":request["source_asset_id"],"source_asset_hash":sha256_bytes(source),
            "source_revision_id":request["source_revision_id"],"target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],
                "base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},
            "route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],"chunk_plan_hash":request["chunk_plan_hash"],
            "lexicon_bindings":lexicons,"output_schema":"style.manufacture-model-response/v1",
            "run_snapshot_hash":request["run_snapshot_hash"],"model_profile_revision_id":request["model_profile_revision_id"],
            "attempt_ids":list(request["provider_attempt_ids"])}
        output,receipt_id=_invoke_model(host,request,ctx,request["model_profile_revision_id"],"manufacture",model_request)
        _model_binding_assertion(output, profile=request["model_profile_revision_id"], route=request["route_id"], attempt_ids=list(request["provider_attempt_ids"]), snapshot_hash=request["run_snapshot_hash"])
        output_core = _without_model_provenance(output)
        if set(output_core)!={"schema","features","constraints","exemplar_hashes","usage"} or output_core["schema"]!="style.manufacture-model-response/v1":raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","manufacture response is not closed")
        output = output_core
        provider_attempts=list(request["provider_attempt_ids"])
        if receipt_id not in provider_attempts:provider_attempts.append(receipt_id)
        pack={"schema":"style-pack/v1","style_pack_id":request["style_pack_id"],"version":request["style_version"],"style_release_id":"","payload_hash":"",
            "source_cas":{"asset_id":request["source_asset_id"],"sha256":request["source_asset_hash"],"revision_id":request["source_revision_id"]},
            "target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],"base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},
            "manufacture_snapshot":{"run_snapshot_hash":request["run_snapshot_hash"],"route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],
                "chunk_plan_hash":request["chunk_plan_hash"],"provider_attempt_ids":provider_attempts,"checkpoint_ids":list(request["checkpoint_ids"]),"usage":output["usage"]},
            "features":output["features"],"lexicon_bindings":[{k:ref[k] for k in ("data_plugin_id","data_release_id","bundle_hash","order")} for ref in request["lexicon_asset_refs"]],
            "constraints":output["constraints"],"exemplar_hashes":output["exemplar_hashes"]}
        pack["payload_hash"]=style_payload_hash(pack);pack["style_release_id"]=style_release_id(pack["style_pack_id"],pack["version"],pack["payload_hash"]);pack=validate_style_pack(pack)
        asset=_upload(host,ctx,canonical_json(pack),"application/json","style-pack")
        self.last_manufacture_state = None
        return _bundle(request,CAPABILITY_MANUFACTURE,[_candidate(request,pack,asset)],self.release_id),[receipt_id]

    def _manufacture_mapped(self, request:Mapping[str,Any], host:HostPort|None, ctx:_RunContext, source:bytes) -> tuple[dict[str,Any],list[str]]:
        plan_id, plan_hash, plan = _chunk_plan_asset(host, request, ctx, source)
        map_profile = str(request.get("map_model_profile_revision_id", request["model_profile_revision_id"]))
        synthesis_profile = str(request.get("synthesis_model_profile_revision_id", request["model_profile_revision_id"]))
        map_attempts = list(request.get("map_attempt_ids", request["provider_attempt_ids"]))
        synthesis_attempts = list(request.get("synthesis_attempt_ids", request["provider_attempt_ids"]))
        records: list[dict[str,Any]] = []
        receipts: list[str] = []
        for index, chunk in enumerate(plan["chunks"]):
            map_request = {
                "schema": "style.manufacture-model-request/v1",
                "phase": "map", "chunk_plan_asset_id": plan_id, "chunk_plan_asset_hash": plan_hash,
                "chunk": dict(chunk), "source_asset_id": request["source_asset_id"],
                "source_asset_hash": request["source_asset_hash"], "source_revision_id": request["source_revision_id"],
                "target_cas": {"workspace_id": request["workspace_id"], "entity_id": request["target_entity_id"],
                               "base_revision_id": request["target_base_revision_id"], "base_content_hash": request["target_base_content_hash"]},
                "route_id": request["route_id"], "prompt_hash": request["prompt_hash"],
                "output_schema_hash": request["output_schema_hash"], "chunk_plan_hash": request["chunk_plan_hash"],
                "lexicon_bindings": [], "output_schema": "style.manufacture-model-response/v1",
                "run_snapshot_hash": request["run_snapshot_hash"], "model_profile_revision_id": map_profile,
                "attempt_ids": list(map_attempts),
            }
            output, receipt_id, output_asset_id, output_asset_hash = _invoke_model_detailed(
                host, request, ctx, map_profile, f"map-{index + 1}", map_request)
            _model_binding_assertion(output, profile=map_profile, route=request["route_id"], attempt_ids=map_attempts, snapshot_hash=request["run_snapshot_hash"])
            output = _without_model_provenance(output)
            if set(output) != {"schema", "features", "constraints", "exemplar_hashes", "usage"} or output["schema"] != "style.manufacture-model-response/v1":
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "map response is not closed")
            # Persist a canonical closed map projection.  The broker-bound
            # response Asset may include optional provenance fields that are
            # stripped from the map schema; retaining its hash here would make
            # the durable map record unverifiable during resume.
            map_output_data = canonical_json(output)
            map_output_id = _upload(host, ctx, map_output_data, "application/json", f"map-output-{index + 1}")
            map_output_hash = sha256_bytes(map_output_data)
            map_record = {
                "schema": "style.manufacture-map/v1", "chunk_id": chunk["chunk_id"], "ordinal": index,
                "chunk_plan_asset_id": plan_id, "chunk_plan_asset_hash": plan_hash,
                "chunk_content_hash": chunk["content_hash"], "map_output_asset_id": map_output_id,
                "map_output_asset_hash": map_output_hash, "map_output": output,
                "model_receipt_id": receipt_id,
            }
            record_data = canonical_json(map_record)
            record_id = _upload(host, ctx, record_data, "application/json", f"map-record-{index + 1}")
            map_record["map_record_asset_id"] = record_id; map_record["map_record_asset_hash"] = sha256_bytes(record_data)
            records.append(map_record); receipts.append(receipt_id)
            # Persist every map atomically; a cancellation observed after this
            # checkpoint is resumable without replaying earlier model calls.
            state = {"schema": "style-manufacturing-resume-state/v2", "binding_hash": ctx.binding_hash,
                     "capability_id": request["capability_id"], "package_hash": self.package_hash,
                     "release_id": self.release_id, "bundle_asset_id": None, "bundle_hash": None,
                     "model_receipt_ids": receipts, "chunk_plan_asset_id": plan_id, "chunk_plan_asset_hash": plan_hash,
                     "map_records": records, "completed_maps": index + 1, "synthesis_receipt_id": None}
            self.last_checkpoint = _checkpoint(host, request, ctx, state)
            if ctx.cancelled is not None and ctx.cancelled.is_set():
                self.last_manufacture_state = state
                return None, receipts

        synthesis_request = {
            "schema": "style.manufacture-model-request/v1", "phase": "synthesis",
            "chunk_plan_asset_id": plan_id, "chunk_plan_asset_hash": plan_hash,
            "map_records": [{k: row[k] for k in ("chunk_id", "ordinal", "map_record_asset_id", "map_record_asset_hash", "map_output_asset_id", "map_output_asset_hash", "model_receipt_id")} for row in records],
            "source_asset_id": request["source_asset_id"], "source_asset_hash": request["source_asset_hash"],
            "source_revision_id": request["source_revision_id"],
            "target_cas": {"workspace_id": request["workspace_id"], "entity_id": request["target_entity_id"],
                           "base_revision_id": request["target_base_revision_id"], "base_content_hash": request["target_base_content_hash"]},
            "route_id": request["route_id"], "prompt_hash": request["prompt_hash"],
            "output_schema_hash": request["output_schema_hash"], "chunk_plan_hash": request["chunk_plan_hash"],
            "lexicon_bindings": [], "output_schema": "style.manufacture-model-response/v1",
            "run_snapshot_hash": request["run_snapshot_hash"], "model_profile_revision_id": synthesis_profile,
            "attempt_ids": list(synthesis_attempts),
        }
        output, synthesis_receipt, _synthesis_asset_id, _synthesis_asset_hash = _invoke_model_detailed(
            host, request, ctx, synthesis_profile, "synthesis", synthesis_request)
        _model_binding_assertion(output, profile=synthesis_profile, route=request["route_id"], attempt_ids=synthesis_attempts, snapshot_hash=request["run_snapshot_hash"])
        output = _without_model_provenance(output)
        if set(output) != {"schema", "features", "constraints", "exemplar_hashes", "usage"} or output["schema"] != "style.manufacture-model-response/v1":
            raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "synthesis response is not closed")
        provider_attempts = list(request["provider_attempt_ids"])
        for receipt_id in [*receipts, synthesis_receipt]:
            if receipt_id not in provider_attempts: provider_attempts.append(receipt_id)
        pack={"schema":"style-pack/v1","style_pack_id":request["style_pack_id"],"version":request["style_version"],"style_release_id":"","payload_hash":"",
            "source_cas":{"asset_id":request["source_asset_id"],"sha256":request["source_asset_hash"],"revision_id":request["source_revision_id"]},
            "target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],"base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},
            "manufacture_snapshot":{"run_snapshot_hash":request["run_snapshot_hash"],"route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],
                "chunk_plan_hash":request["chunk_plan_hash"],"provider_attempt_ids":provider_attempts,"checkpoint_ids":list(request["checkpoint_ids"]),"usage":output["usage"]},
            "features":output["features"],"lexicon_bindings":[],"constraints":output["constraints"],"exemplar_hashes":output["exemplar_hashes"]}
        pack["payload_hash"] = style_payload_hash(pack); pack["style_release_id"] = style_release_id(pack["style_pack_id"], pack["version"], pack["payload_hash"]); pack = validate_style_pack(pack)
        asset = _upload(host, ctx, canonical_json(pack), "application/json", "style-pack")
        self.last_manufacture_state = {"schema": "style-manufacturing-resume-state/v2", "binding_hash": ctx.binding_hash,
            "capability_id": request["capability_id"], "package_hash": self.package_hash, "release_id": self.release_id,
            "bundle_asset_id": None, "bundle_hash": None, "model_receipt_ids": provider_attempts,
            "chunk_plan_asset_id": plan_id, "chunk_plan_asset_hash": plan_hash, "map_records": records,
            "completed_maps": len(records), "synthesis_receipt_id": synthesis_receipt}
        return _bundle(request,CAPABILITY_MANUFACTURE,[_candidate(request,pack,asset)],self.release_id), provider_attempts

    def qualify(self,request:Mapping[str,Any],host:HostPort|None,ctx:_RunContext)->tuple[dict[str,Any],list[str]]:
        # All qualification inputs are immutable, canonical Assets.  Parsing
        # an Asset and retaining only its hash is insufficient because a model
        # broker could receive a different representation of the same object.
        pack=validate_style_pack(_load_json_asset(host,request["style_pack_asset_id"],request["style_pack_asset_hash"]))
        if request["manufacturing_route_id"] != pack["manufacture_snapshot"]["route_id"]:
            raise StyleManufacturingWorkerError("QUALIFICATION_PATH_INVALID","qualification manufacturer route differs from exact style release")
        if request["manufacturing_attempt_ids"] != pack["manufacture_snapshot"]["provider_attempt_ids"]:
            raise StyleManufacturingWorkerError("QUALIFICATION_PATH_INVALID","qualification manufacturer attempts differ from exact style release")
        rubric = _load_json_asset(host,request["rubric_asset_id"],request["rubric_asset_hash"])
        if not isinstance(rubric, Mapping):
            raise StyleManufacturingWorkerError("RUBRIC_INVALID", "quality rubric Asset must be an object")
        rubric_raw = canonical_json(rubric)
        sealed_case_id, sealed_case_hash, sealed_case = _sealed_case_asset(host, request, ctx, pack, rubric_raw)
        frozen_inputs = _frozen_model_inputs(style_pack=pack,
            style_pack_asset_id=request["style_pack_asset_id"],style_pack_asset_hash=request["style_pack_asset_hash"],
            sealed_case=sealed_case,sealed_case_asset_id=sealed_case_id,sealed_case_asset_hash=sealed_case_hash,
            rubric=rubric,rubric_asset_id=request["rubric_asset_id"],rubric_asset_hash=request["rubric_asset_hash"])
        writer_request={"schema":"style.qualify.writer-model-request/v1","style_release_id":pack["style_release_id"],"style_payload_hash":pack["payload_hash"],
            "style_pack_asset_id":request["style_pack_asset_id"],"style_pack_asset_hash":request["style_pack_asset_hash"],
            "sealed_case_asset_id":sealed_case_id,"sealed_case_asset_hash":sealed_case_hash,
            "rubric_asset_id":request["rubric_asset_id"],"rubric_asset_hash":request["rubric_asset_hash"],
            "rubric_hash":sha256_bytes(rubric_raw),"sealed_case_ids":[x["case_id"] for x in sealed_case["cases"]],
            "style_pack":frozen_inputs["style_pack"],"sealed_case":frozen_inputs["sealed_case"],"rubric":frozen_inputs["rubric"],
            "frozen_inputs":frozen_inputs,"route_id":request["writer_route_id"],
            "model_profile_revision_id":request["writer_model_profile_revision_id"],"run_snapshot_hash":request["run_snapshot_hash"],
            "attempt_ids":list(request["writer_attempt_ids"]),"output_schema":"style.qualify.writer-model-response/v1"}
        writer,writer_receipt,writer_output_asset_id,writer_output_asset_hash=_invoke_model_detailed(host,request,ctx,request["writer_model_profile_revision_id"],"qualification-writer",writer_request)
        _model_binding_assertion(writer, profile=request["writer_model_profile_revision_id"], route=request["writer_route_id"], attempt_ids=list(request["writer_attempt_ids"]), snapshot_hash=request["run_snapshot_hash"])
        writer_allowed={"schema","cases"} | ({key for key in _MODEL_PROVENANCE_FIELDS if key in writer})
        if set(writer)!=writer_allowed or writer["schema"]!="style.qualify.writer-model-response/v1" or not isinstance(writer["cases"],list) or len(writer["cases"])!=3:raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","writer qualification response invalid")
        writer_by={x.get("case_id"):x for x in writer["cases"] if isinstance(x,Mapping)}
        expected_case_ids=[x["case_id"] for x in sealed_case["cases"]]
        if list(writer_by) != expected_case_ids or len(writer_by)!=3:raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","writer qualification case identity mismatch")
        sealed=[]
        for case in sealed_case["cases"]:
            row=writer_by.get(case["case_id"])
            if not isinstance(row,Mapping) or set(row)!={"case_id","output_text"} or not isinstance(row["output_text"],str):
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","writer output is not closed")
            text=row["output_text"].strip()
            if not _writer_text_is_qualifying(
                text,
                case=case,
                rubric=rubric,
                style_pack=pack,
            ):
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","writer output is too poor to qualify")
            case_output_data = canonical_json({"schema":"style.qualify.writer-output/v1",
                "case_id":case["case_id"],"style_release_id":pack["style_release_id"],
                "style_payload_hash":pack["payload_hash"],"sealed_case_asset_hash":sealed_case_hash,
                "rubric_asset_hash":request["rubric_asset_hash"],"output_text":row["output_text"]})
            case_output_asset_id = _upload(host, ctx, case_output_data, "application/json", "writer-output-" + str(case["case_id"]))
            case_output_asset_hash = sha256_bytes(case_output_data)
            sealed.append({"case_id":case["case_id"],"writer_output_hash":sha256_bytes(row["output_text"].encode("utf-8")),
                           "writer_output_asset_id":case_output_asset_id,"writer_output_asset_hash":case_output_asset_hash})
        bound_writer = _load_json_asset(host, writer_output_asset_id, writer_output_asset_hash)
        if bound_writer != writer:
            raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "writer output Asset changed after immutable publication")
        for row in sealed:
            case_output = _load_json_asset(host, row["writer_output_asset_id"], row["writer_output_asset_hash"])
            if (not isinstance(case_output, Mapping) or case_output.get("schema") != "style.qualify.writer-output/v1"
                    or case_output.get("case_id") != row["case_id"]
                 or sha256_bytes(str(case_output.get("output_text", "")).encode("utf-8")) != row["writer_output_hash"]):
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "writer case output Asset is not exact")
        rubric_reloaded = _load_json_asset(host, request["rubric_asset_id"], request["rubric_asset_hash"])
        if rubric_reloaded != rubric:
            raise StyleManufacturingWorkerError("RUBRIC_INVALID", "rubric Asset changed during qualification")
        writer_output_bindings = []
        for row in sealed:
            case_output = _load_json_asset(host, row["writer_output_asset_id"], row["writer_output_asset_hash"])
            writer_output_bindings.append(_frozen_input_binding(
                row["writer_output_asset_id"],row["writer_output_asset_hash"],case_output,
                f"writer_output.{row['case_id']}"))
        reviewer_frozen_inputs = dict(frozen_inputs)
        reviewer_frozen_inputs["writer_outputs"] = writer_output_bindings
        review_request={"schema":"style.qualify.reviewer-model-request/v1","style_release_id":pack["style_release_id"],"style_payload_hash":pack["payload_hash"],
            "style_pack_asset_id":request["style_pack_asset_id"],"style_pack_asset_hash":request["style_pack_asset_hash"],
            "sealed_case_asset_id":sealed_case_id,"sealed_case_asset_hash":sealed_case_hash,
            "rubric_asset_id":request["rubric_asset_id"],"rubric_asset_hash":request["rubric_asset_hash"],
            "rubric_hash":sha256_bytes(rubric_raw),
            "writer_output_asset_id":writer_output_asset_id,"writer_output_asset_hash":writer_output_asset_hash,
            "sealed_outputs":[{"case_id":x["case_id"],"writer_output_hash":x["writer_output_hash"],
                               "writer_output_asset_id":x["writer_output_asset_id"],"writer_output_asset_hash":x["writer_output_asset_hash"],
                               "writer_output":next(item for item in writer_output_bindings if item["asset_id"]==x["writer_output_asset_id"])} for x in sealed],
            "style_pack":frozen_inputs["style_pack"],"sealed_case":frozen_inputs["sealed_case"],"rubric":frozen_inputs["rubric"],
            "writer_output":_frozen_input_binding(writer_output_asset_id,writer_output_asset_hash,writer,"writer_output"),
            "writer_outputs":writer_output_bindings,"frozen_inputs":reviewer_frozen_inputs,
            "route_id":request["reviewer_route_id"],"model_profile_revision_id":request["reviewer_model_profile_revision_id"],
            "run_snapshot_hash":request["run_snapshot_hash"],"attempt_ids":list(request["reviewer_attempt_ids"]),
            "output_schema":"style.qualify.reviewer-model-response/v1"}
        reviewer,review_receipt,review_output_asset_id,review_output_asset_hash=_invoke_model_detailed(host,request,ctx,request["reviewer_model_profile_revision_id"],"qualification-reviewer",review_request)
        _model_binding_assertion(reviewer, profile=request["reviewer_model_profile_revision_id"], route=request["reviewer_route_id"], attempt_ids=list(request["reviewer_attempt_ids"]), snapshot_hash=request["run_snapshot_hash"])
        if _load_json_asset(host, review_output_asset_id, review_output_asset_hash) != reviewer:
            raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "review output Asset changed after immutable publication")
        reviewer_allowed={"schema","cases"} | ({key for key in _MODEL_PROVENANCE_FIELDS if key in reviewer})
        if set(reviewer)!=reviewer_allowed or reviewer["schema"]!="style.qualify.reviewer-model-response/v1" or not isinstance(reviewer["cases"],list) or len(reviewer["cases"])!=3:raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","reviewer qualification response invalid")
        review_by={x.get("case_id"):x for x in reviewer["cases"] if isinstance(x,Mapping)}
        if list(review_by) != expected_case_ids or len(review_by)!=3:raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","reviewer qualification case identity mismatch")
        case_results=[]
        for row in sealed:
            case_id=row["case_id"];review=review_by.get(case_id)
            if not isinstance(review,Mapping) or set(review)!={"case_id","score_0_100","passed"}:
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","review output is not closed")
            score=review["score_0_100"];passed=review["passed"]
            if isinstance(score,bool) or not isinstance(score,int) or not 0<=score<=100 or not isinstance(passed,bool):
                raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID","review score/status invalid")
            if score < 70: passed=False
            case_results.append({"case_id":case_id,"writer_output_hash":row["writer_output_hash"],
                "review_output_hash":hash_json("style-qualification-review/v1",dict(review)),"score_0_100":score,"passed":passed})
        writer_attempts=list(request["writer_attempt_ids"]);reviewer_attempts=list(request["reviewer_attempt_ids"])
        if writer_receipt not in writer_attempts:writer_attempts.append(writer_receipt)
        if review_receipt not in reviewer_attempts:reviewer_attempts.append(review_receipt)
        receipt=build_qualification_receipt(receipt_id=_derived_id("style-qualification",pack["style_release_id"],request["run_snapshot_hash"]),style_pack=pack,
            qualification_run_snapshot_hash=request["run_snapshot_hash"],rubric_hash=request["rubric_asset_hash"],manufacturing_route_id=request["manufacturing_route_id"],
            writer_route_id=request["writer_route_id"],reviewer_route_id=request["reviewer_route_id"],manufacturing_attempt_ids=list(request["manufacturing_attempt_ids"]),
            writer_attempt_ids=writer_attempts,reviewer_attempt_ids=reviewer_attempts,case_results=case_results,created_at=request["created_at"],
            style_pack_asset_id=request["style_pack_asset_id"],style_pack_asset_hash=request["style_pack_asset_hash"],
             sealed_case_asset_id=sealed_case_id,sealed_case_asset_hash=sealed_case_hash,
             rubric_asset_id=request["rubric_asset_id"],rubric_asset_hash=request["rubric_asset_hash"],
             writer_output_asset_ids=[x["writer_output_asset_id"] for x in sealed],
             writer_output_asset_hashes=[x["writer_output_asset_hash"] for x in sealed],
             review_output_asset_id=review_output_asset_id,review_output_asset_hash=review_output_asset_hash)
        self.last_qualification_receipt=receipt
        item=_diagnostic(request,ctx,host,receipt,"STYLE_QUALIFIED" if receipt["decision"]=="pass" else "STYLE_NOT_QUALIFIED",
            "exact style release passed independent qualification" if receipt["decision"]=="pass" else "style release did not pass qualification",
            severity="info" if receipt["decision"]=="pass" else "warning")
        return _bundle(request,CAPABILITY_QUALIFY,[item],self.release_id),[writer_receipt,review_receipt]

    def package(self,request:Mapping[str,Any],host:HostPort|None,ctx:_RunContext)->tuple[dict[str,Any],list[str]]:
        # Use the same canonical JSON Asset loader as qualification and the
        # runtime plugin.  Packaging must not accept a semantically equivalent
        # but differently encoded style/receipt Asset that manufacturing would
        # reject under the shared canonical validator.
        pack=validate_style_pack(_load_json_asset(host,request["style_pack_asset_id"],request["style_pack_asset_hash"]))
        receipt=validate_qualification_receipt(_load_json_asset(host,request["qualification_receipt_asset_id"],request["qualification_receipt_asset_hash"]),pack["style_release_id"])
        if not exact_release_eligible(pack,receipt):raise StyleManufacturingWorkerError("STYLE_RELEASE_NOT_QUALIFIED","only the exact qualified style release can be packaged")
        body={"schema":"style-data-package/v1","package_kind":"data","data_plugin_id":request["data_plugin_id"],"version":request["data_version"],"format_id":"style-pack/v1",
            "style_pack":pack,"qualification_receipt":receipt,"identity_separation":{"code_plugin_id":PLUGIN_ID,"style_release_id":pack["style_release_id"],"skill_release_id":None}}
        body["package_hash"]=hash_json("style-data-package/v1",body)
        body["data_release_id"]=sha256_bytes(f"plotpilot-release/v1\n{request['data_plugin_id']}\n{request['data_version']}\n{body['package_hash']}\n".encode())
        asset=_upload(host,ctx,canonical_json(body),"application/json","style-data-package")
        return _bundle(request,CAPABILITY_PACKAGE,[_artifact(request,body,asset)],self.release_id),[]

    def _resume_mapped(self, request:Mapping[str,Any], host:HostPort|None, ctx:_RunContext,
                       active:_ActiveRun, state:Mapping[str,Any]) -> dict[str,Any]|None:
        """Continue a materialized map/synthesis run from its last checkpoint."""
        if state.get("binding_hash") != ctx.binding_hash or state.get("capability_id") != request["capability_id"] or state.get("package_hash") != self.package_hash or state.get("release_id") != self.release_id:
            raise StyleManufacturingWorkerError("RESUME_INVALID", "mapped resume state binding mismatch")
        if state.get("bundle_asset_id") is not None:
            bundle_asset_id = _id(state.get("bundle_asset_id"), "bundle_asset_id")
            bundle_hash = _hash(state.get("bundle_hash"), "bundle_hash")
            bundle = _strict_json(_read_asset(host, bundle_asset_id, bundle_hash))
            try:
                assert _verify_result_bundle_sdk is not None
                _verify_result_bundle_sdk(bundle, snapshot_workspace_id=request["workspace_id"], snapshot_hash_value=request["run_snapshot_hash"])
            except Exception as exc:
                raise StyleManufacturingWorkerError("RESUME_INVALID", f"mapped Result Bundle binding failed: {exc}") from exc
            if bundle.get("producer") != _producer(request, CAPABILITY_MANUFACTURE, self.release_id):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "mapped Result Bundle producer mismatch")
            receipts = state.get("model_receipt_ids")
            if not isinstance(receipts, list) or not receipts or len(receipts) != len(set(receipts)):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "mapped model receipt state is invalid")
            if active.cancelled.is_set():
                self.last_receipt = _receipt(request, CAPABILITY_MANUFACTURE, self.package_hash, self.release_id, None, receipts, [])
                _complete(host, request, ctx, "cancelled", None, None, None, ctx.last_local_seq + 1)
                return None
            stage_key, staged = self._stage(request, host, ctx, bundle_asset_id, bundle_hash, bundle)
            self.last_receipt = _receipt(request, CAPABILITY_MANUFACTURE, self.package_hash, self.release_id, bundle, receipts, staged)
            _complete(host, request, ctx, "succeeded", bundle_asset_id, stage_key, None, ctx.last_local_seq + 1)
            return bundle
        plan_id = _id(state.get("chunk_plan_asset_id"), "chunk_plan_asset_id")
        plan_hash = _hash(state.get("chunk_plan_asset_hash"), "chunk_plan_asset_hash")
        source = _read_asset(host, request["source_asset_id"], request["source_asset_hash"])
        _plan_id, _plan_hash, plan = _chunk_plan_asset(host, request, ctx, source)
        if plan_id != _plan_id or plan_hash != _plan_hash:
            raise StyleManufacturingWorkerError("RESUME_INVALID", "mapped resume plan identity mismatch")
        raw_records = state.get("map_records")
        if not isinstance(raw_records, list) or state.get("completed_maps") != len(raw_records) or len(raw_records) > len(plan["chunks"]):
            raise StyleManufacturingWorkerError("RESUME_INVALID", "mapped resume records are incomplete")
        records: list[dict[str,Any]] = []
        receipts: list[str] = []
        for index, record in enumerate(raw_records):
            if not isinstance(record, Mapping): raise StyleManufacturingWorkerError("RESUME_INVALID", "map record is not an object")
            required = {"schema","chunk_id","ordinal","chunk_plan_asset_id","chunk_plan_asset_hash","chunk_content_hash","map_output_asset_id","map_output_asset_hash","map_output","model_receipt_id","map_record_asset_id","map_record_asset_hash"}
            if set(record) != required or record["schema"] != "style.manufacture-map/v1" or record["ordinal"] != index:
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map record is not closed")
            if record["chunk_plan_asset_id"] != plan_id or record["chunk_plan_asset_hash"] != plan_hash:
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map record plan binding mismatch")
            expected_chunk = plan["chunks"][index]
            if (record["chunk_id"], record["chunk_content_hash"]) != (expected_chunk["chunk_id"], expected_chunk["content_hash"]):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map record chunk binding mismatch")
            _hash(record["chunk_content_hash"], "chunk_content_hash"); _id(record["map_output_asset_id"], "map_output_asset_id"); _hash(record["map_output_asset_hash"], "map_output_asset_hash"); _id(record["model_receipt_id"], "model_receipt_id"); _id(record["map_record_asset_id"], "map_record_asset_id"); _hash(record["map_record_asset_hash"], "map_record_asset_hash")
            if sha256_bytes(canonical_json(record["map_output"])) != record["map_output_asset_hash"]:
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map output hash mismatch")
            map_output_asset = _load_json_asset(host, record["map_output_asset_id"], record["map_output_asset_hash"])
            if map_output_asset != record["map_output"]:
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map output Asset differs from the map record")
            if (set(record["map_output"]) != {"schema", "features", "constraints", "exemplar_hashes", "usage"}
                    or record["map_output"]["schema"] != "style.manufacture-model-response/v1"):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map output is not closed")
            record_projection = {key: record[key] for key in required if key not in {"map_record_asset_id", "map_record_asset_hash"}}
            record_bytes = _read_asset(host, record["map_record_asset_id"], record["map_record_asset_hash"])
            if sha256_bytes(record_bytes) != record["map_record_asset_hash"] or _strict_json(record_bytes) != record_projection:
                raise StyleManufacturingWorkerError("RESUME_INVALID", "map record Asset hash mismatch")
            records.append(dict(record)); receipts.append(record["model_receipt_id"])
        map_profile = str(request.get("map_model_profile_revision_id", request["model_profile_revision_id"]))
        synthesis_profile = str(request.get("synthesis_model_profile_revision_id", request["model_profile_revision_id"]))
        map_attempts = list(request.get("map_attempt_ids", request["provider_attempt_ids"]))
        synthesis_attempts = list(request.get("synthesis_attempt_ids", request["provider_attempt_ids"]))
        for index in range(len(records), len(plan["chunks"])):
            chunk = plan["chunks"][index]
            map_request = {"schema":"style.manufacture-model-request/v1","phase":"map","chunk_plan_asset_id":plan_id,"chunk_plan_asset_hash":plan_hash,"chunk":dict(chunk),
                 "source_asset_id":request["source_asset_id"],"source_asset_hash":request["source_asset_hash"],"source_revision_id":request["source_revision_id"],
                 "target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],"base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},
                 "route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],"chunk_plan_hash":request["chunk_plan_hash"],"lexicon_bindings":[],"output_schema":"style.manufacture-model-response/v1",
                 "run_snapshot_hash":request["run_snapshot_hash"],"model_profile_revision_id":map_profile,"attempt_ids":list(map_attempts)}
            output, receipt_id, output_asset_id, output_asset_hash = _invoke_model_detailed(host, request, ctx, map_profile, f"map-{index + 1}", map_request)
            _model_binding_assertion(output, profile=map_profile, route=request["route_id"], attempt_ids=map_attempts, snapshot_hash=request["run_snapshot_hash"])
            output = _without_model_provenance(output)
            if output.get("schema") != "style.manufacture-model-response/v1": raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "map response schema mismatch")
            # The broker-bound Asset may carry optional provenance fields that
            # are intentionally removed from the closed map response. Persist
            # a second immutable canonical Asset for that exact projection;
            # otherwise resume would compare the stripped map to a hash that
            # still includes the provenance extension.
            map_output_data = canonical_json(output)
            map_output_id = _upload(host, ctx, map_output_data, "application/json", f"map-output-{index + 1}")
            map_output_hash = sha256_bytes(map_output_data)
            record = {"schema":"style-manufacture-map/v1","chunk_id":chunk["chunk_id"],"ordinal":index,"chunk_plan_asset_id":plan_id,"chunk_plan_asset_hash":plan_hash,"chunk_content_hash":chunk["content_hash"],"map_output_asset_id":map_output_id,"map_output_asset_hash":map_output_hash,"map_output":output,"model_receipt_id":receipt_id}
            record_data = canonical_json(record); record_id = _upload(host,ctx,record_data,"application/json",f"map-record-{index + 1}"); record["map_record_asset_id"] = record_id; record["map_record_asset_hash"] = sha256_bytes(record_data)
            records.append(record); receipts.append(receipt_id)
            state_update={"schema":"style-manufacturing-resume-state/v2","binding_hash":ctx.binding_hash,"capability_id":request["capability_id"],"package_hash":self.package_hash,"release_id":self.release_id,"bundle_asset_id":None,"bundle_hash":None,"model_receipt_ids":receipts,"chunk_plan_asset_id":plan_id,"chunk_plan_asset_hash":plan_hash,"map_records":records,"completed_maps":len(records),"synthesis_receipt_id":None}
            self.last_checkpoint = _checkpoint(host, request, ctx, state_update)
            if active.cancelled.is_set():
                self.last_manufacture_state = state_update
                self.last_receipt = _receipt(request, CAPABILITY_MANUFACTURE, self.package_hash, self.release_id, None, receipts, [])
                _complete(host, request, ctx, "cancelled", None, None, None, ctx.last_local_seq + 1)
                return None
        # Re-run only synthesis (all map outputs are immutable and verified).
        synthesis_request={"schema":"style.manufacture-model-request/v1","phase":"synthesis","chunk_plan_asset_id":plan_id,"chunk_plan_asset_hash":plan_hash,
             "map_records":[{k:row[k] for k in ("chunk_id","ordinal","map_record_asset_id","map_record_asset_hash","map_output_asset_id","map_output_asset_hash","model_receipt_id")} for row in records],
             "source_asset_id":request["source_asset_id"],"source_asset_hash":request["source_asset_hash"],"source_revision_id":request["source_revision_id"],"target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],"base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},"route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],"chunk_plan_hash":request["chunk_plan_hash"],"lexicon_bindings":[],"output_schema":"style.manufacture-model-response/v1"}
        synthesis_request.update({"run_snapshot_hash":request["run_snapshot_hash"],"model_profile_revision_id":synthesis_profile,"attempt_ids":list(synthesis_attempts)})
        output, synthesis_receipt, _synthesis_asset_id, _synthesis_asset_hash = _invoke_model_detailed(host, request, ctx, synthesis_profile, "synthesis", synthesis_request)
        _model_binding_assertion(output, profile=synthesis_profile, route=request["route_id"], attempt_ids=synthesis_attempts, snapshot_hash=request["run_snapshot_hash"])
        output = _without_model_provenance(output)
        if set(output) != {"schema","features","constraints","exemplar_hashes","usage"} or output["schema"] != "style.manufacture-model-response/v1": raise StyleManufacturingWorkerError("MODEL_OUTPUT_INVALID", "synthesis response is not closed")
        provider_attempts=list(request["provider_attempt_ids"])
        for receipt_id in [*receipts,synthesis_receipt]:
            if receipt_id not in provider_attempts: provider_attempts.append(receipt_id)
        pack={"schema":"style-pack/v1","style_pack_id":request["style_pack_id"],"version":request["style_version"],"style_release_id":"","payload_hash":"","source_cas":{"asset_id":request["source_asset_id"],"sha256":request["source_asset_hash"],"revision_id":request["source_revision_id"]},"target_cas":{"workspace_id":request["workspace_id"],"entity_id":request["target_entity_id"],"base_revision_id":request["target_base_revision_id"],"base_content_hash":request["target_base_content_hash"]},"manufacture_snapshot":{"run_snapshot_hash":request["run_snapshot_hash"],"route_id":request["route_id"],"prompt_hash":request["prompt_hash"],"output_schema_hash":request["output_schema_hash"],"chunk_plan_hash":request["chunk_plan_hash"],"provider_attempt_ids":provider_attempts,"checkpoint_ids":list(request["checkpoint_ids"]),"usage":output["usage"]},"features":output["features"],"lexicon_bindings":[],"constraints":output["constraints"],"exemplar_hashes":output["exemplar_hashes"]}
        pack["payload_hash"]=style_payload_hash(pack); pack["style_release_id"]=style_release_id(pack["style_pack_id"],pack["version"],pack["payload_hash"]); pack=validate_style_pack(pack); pack_asset=_upload(host,ctx,canonical_json(pack),"application/json","style-pack")
        bundle=_bundle(request,CAPABILITY_MANUFACTURE,[_candidate(request,pack,pack_asset)],self.release_id)
        self.last_manufacture_state={"schema":"style-manufacturing-resume-state/v2","binding_hash":ctx.binding_hash,"capability_id":request["capability_id"],"package_hash":self.package_hash,"release_id":self.release_id,"bundle_asset_id":None,"bundle_hash":None,"model_receipt_ids":provider_attempts,"chunk_plan_asset_id":plan_id,"chunk_plan_asset_hash":plan_hash,"map_records":records,"completed_maps":len(records),"synthesis_receipt_id":synthesis_receipt}
        raw_bundle=canonical_json(bundle); bundle_asset_id=_upload(host,ctx,raw_bundle,"application/json","result-bundle",operation_key=_derived_id("result-upload-operation",ctx.binding_hash),upload_id=_derived_id("result-upload",ctx.binding_hash)); state_final=dict(self.last_manufacture_state); state_final.update({"bundle_asset_id":bundle_asset_id,"bundle_hash":sha256_bytes(raw_bundle),"model_receipt_ids":provider_attempts}); self.last_checkpoint=_checkpoint(host,request,ctx,state_final)
        if active.cancelled.is_set(): self.last_receipt=_receipt(request,CAPABILITY_MANUFACTURE,self.package_hash,self.release_id,None,provider_attempts,[]); _complete(host,request,ctx,"cancelled",None,None,None,ctx.last_local_seq+1); return None
        stage_key,staged=self._stage(request,host,ctx,bundle_asset_id,sha256_bytes(raw_bundle),bundle); self.last_receipt=_receipt(request,CAPABILITY_MANUFACTURE,self.package_hash,self.release_id,bundle,provider_attempts,staged); _complete(host,request,ctx,"succeeded",bundle_asset_id,stage_key,None,ctx.last_local_seq+1); return bundle

    def _resume(self,request:Mapping[str,Any],host:HostPort|None,ctx:_RunContext,active:_ActiveRun)->dict[str,Any]|None:
        checkpoint=_load_json_asset(host,request["resume_checkpoint_asset_id"],request["resume_checkpoint_asset_hash"]);assert _verify_checkpoint_sdk is not None
        _verify_checkpoint_sdk(checkpoint,expected_snapshot_hash=request["run_snapshot_hash"])
        if (checkpoint.get("state_asset_id") != request["resume_state_asset_id"]
                or checkpoint.get("unit_set_hash") != request["resume_state_asset_hash"]):
            raise StyleManufacturingWorkerError("RESUME_INVALID", "checkpoint does not bind the requested resume state")
        state=_load_json_asset(host,request["resume_state_asset_id"],request["resume_state_asset_hash"])
        if isinstance(state, Mapping) and state.get("schema") == "style-manufacturing-resume-state/v2":
            try:
                ctx.last_checkpoint_seq = int(checkpoint.get("checkpoint_seq", 0))
            except (TypeError, ValueError):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "checkpoint sequence is invalid")
            completed_maps = state.get("completed_maps")
            if (isinstance(completed_maps, bool) or not isinstance(completed_maps, int)
                    or checkpoint.get("completed_units") != completed_maps
                    or completed_maps < 0 or completed_maps > request["total_units"]):
                raise StyleManufacturingWorkerError("RESUME_INVALID", "checkpoint and mapped state progress differ")
            return self._resume_mapped(request, host, ctx, active, state)
        if not isinstance(state,Mapping) or set(state)!={"schema","binding_hash","capability_id","package_hash","release_id","bundle_asset_id","bundle_hash","model_receipt_ids"} or state["schema"]!="style-manufacturing-resume-state/v1" or state["binding_hash"]!=ctx.binding_hash or state["capability_id"]!=request["capability_id"] or state["package_hash"]!=self.package_hash or state["release_id"]!=self.release_id:raise StyleManufacturingWorkerError("RESUME_INVALID","resume state binding mismatch")
        raw=_read_asset(host,_id(state["bundle_asset_id"],"bundle_asset_id"),_hash(state["bundle_hash"],"bundle_hash"));bundle=_strict_json(raw)
        assert _verify_result_bundle_sdk is not None;_verify_result_bundle_sdk(bundle,snapshot_workspace_id=request["workspace_id"] if bundle.get("contract_id")=="candidate-batch/v1" else None,snapshot_hash_value=request["run_snapshot_hash"])
        if active.cancelled.is_set():_complete(host,request,ctx,"cancelled",None,None,None,1);return None
        stage_key=None;staged=[]
        if bundle["contract_id"]=="candidate-batch/v1":stage_key,staged=self._stage(request,host,ctx,state["bundle_asset_id"],state["bundle_hash"],bundle)
        self.last_receipt=_receipt(request,request["capability_id"],self.package_hash,self.release_id,bundle,list(state["model_receipt_ids"]),staged)
        _complete(host,request,ctx,"succeeded",state["bundle_asset_id"],stage_key,None,2);return bundle

    def _stage(self,request:Mapping[str,Any],host:HostPort|None,ctx:_RunContext,bundle_asset_id:str,bundle_hash:str,bundle:Mapping[str,Any])->tuple[str,list[str]]:
        key=_derived_id("candidate-stage",ctx.binding_hash,bundle_hash)
        response=_host_call(host,"host.candidate.stage/v1",{"operation_key":key,"result_bundle_asset_id":bundle_asset_id,"input_snapshot_hash":request["run_snapshot_hash"]})
        expected=[x["item_id"] for x in bundle["items"]];rows=response.get("staged_items")
        if response.get("accepted") is not True or not isinstance(rows,list) or [x.get("item_id") for x in rows]!=expected:raise StyleManufacturingWorkerError("CANDIDATE_STAGE_ERROR","stage acknowledgement mismatch")
        return key,expected

    def _failure(self,request:Mapping[str,Any],capability:str,ctx:_RunContext|None,host:HostPort|None,error:BaseException)->dict[str,Any]|None:
        if isinstance(error,TerminalContractError):raise error
        if ctx is None:return None
        code=str(getattr(error,"code","INTERNAL_ERROR"));details={"schema":"style-manufacturing-failure/v1","code":code,"message":str(error)[:1024],"retryable":bool(getattr(error,"retryable",False))}
        try:
            item=_diagnostic(request,ctx,host,details,code,str(error)[:1024],status="failed",severity="error");bundle=_bundle(request,capability,[item],self.release_id,failure=True)
            raw=canonical_json(bundle);asset=_upload(host,ctx,raw,"application/json","failure-bundle");self.last_receipt=_receipt(request,capability,self.package_hash,self.release_id,bundle,[],[])
            _complete(host,request,ctx,"failed",asset,None,item["details_asset_id"],ctx.last_local_seq+1);return bundle
        except TerminalContractError:raise
        except Exception as exc:raise StyleManufacturingWorkerError("FAILURE_TERMINAL_ERROR",str(exc)) from exc

    def run(self,request:Mapping[str,Any],host:HostPort|None=None)->dict[str,Any]|None:
        value=dict(request) if isinstance(request,Mapping) else {};capability=value.get("capability_id");ctx=None;active=None
        if capability not in CAPABILITIES: return None
        try:
            normalized,ctx=_context(value,capability)
            if normalized["operation"]=="cancel":return self.cancel(normalized)
            if capability!=CAPABILITY_PACKAGE:active=_DISPATCHER.begin(normalized["worker_run_id"],ctx.binding_hash)
            if active is not None:
                # Mapped manufacture checks cancellation at each durable map
                # checkpoint through the shared context.  Bind the dispatcher
                # event here so a cancel request cannot be lost between maps.
                ctx.cancelled = active.cancelled
            if normalized["operation"]=="resume":assert active is not None;return self._resume(normalized,host,ctx,active)
            if normalized["operation"]=="run":_event(host,ctx,"style-manufacturing.started",None,1)
            if capability==CAPABILITY_MANUFACTURE:bundle,model_receipts=self.manufacture(normalized,host,ctx)
            elif capability==CAPABILITY_QUALIFY:bundle,model_receipts=self.qualify(normalized,host,ctx)
            else:bundle,model_receipts=self.package(normalized,host,ctx)
            if bundle is None:
                # A map checkpoint observed cancellation before synthesis;
                # terminal cancellation is dispatched without staging a
                # partial Candidate.
                self.last_receipt=_receipt(normalized,capability,self.package_hash,self.release_id,None,model_receipts,[])
                _complete(host,normalized,ctx,"cancelled",None,None,None,ctx.last_local_seq+1)
                return None
            if normalized["operation"]=="validate":self.last_receipt=None;return bundle
            raw=canonical_json(bundle);bundle_asset=_upload(host,ctx,raw,"application/json","result-bundle",operation_key=_derived_id("result-upload-operation",ctx.binding_hash),upload_id=_derived_id("result-upload",ctx.binding_hash))
            if capability!=CAPABILITY_PACKAGE:
                state=dict(self.last_manufacture_state or {"schema":"style-manufacturing-resume-state/v1"})
                state.update({"schema":state.get("schema","style-manufacturing-resume-state/v1"),"binding_hash":ctx.binding_hash,"capability_id":capability,"package_hash":self.package_hash,"release_id":self.release_id,
                    "bundle_asset_id":bundle_asset,"bundle_hash":sha256_bytes(raw),"model_receipt_ids":model_receipts})
                self.last_checkpoint=_checkpoint(host,normalized,ctx,state)
                if active is not None and active.cancelled.is_set():self.last_receipt=_receipt(normalized,capability,self.package_hash,self.release_id,None,model_receipts,[]);_complete(host,normalized,ctx,"cancelled",None,None,None,ctx.last_local_seq+1);return None
            stage_key=None;staged=[]
            if bundle["contract_id"]=="candidate-batch/v1":stage_key,staged=self._stage(normalized,host,ctx,bundle_asset,sha256_bytes(raw),bundle)
            self.last_receipt=_receipt(normalized,capability,self.package_hash,self.release_id,bundle,model_receipts,staged)
            _complete(host,normalized,ctx,"succeeded",bundle_asset,stage_key,None,ctx.last_local_seq+1);return bundle
        except Exception as exc:return self._failure(value,capability,ctx,host,exc)
        finally:
            if active is not None:_DISPATCHER.finish(active)


def capability_descriptor(capability_id:str|None=None)->dict[str,Any]:
    return DESCRIPTORS[capability_id or CAPABILITY_MANUFACTURE]


PACKAGE_HASH=str(load_runtime_identity()["package_hash"]);RELEASE_ID=str(load_runtime_identity()["release_id"]);DESCRIPTORS=descriptors(RELEASE_ID);_RUNTIME=StyleManufacturingPlugin()


def main(request:Mapping[str,Any]|None=None,host:HostPort|None=None)->dict[str,Any]|None:
    if request is None:return capability_descriptor()
    return _RUNTIME.run(request,host)
