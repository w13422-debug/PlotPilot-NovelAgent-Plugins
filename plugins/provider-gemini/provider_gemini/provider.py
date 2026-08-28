"""Gemini Generative Language adapter with a closed, Core-bound provider boundary."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from numbers import Real
from typing import Any, Iterable, Iterator, Mapping, Protocol
from urllib import request as urllib_request
from urllib.parse import urlsplit

from .contract import ProviderSchemaError, validate_descriptor_schema_references, validate_schema
from .package_identity import load_runtime_identity


class ProviderError(RuntimeError):
    """A fail-closed provider or Host contract error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class _Cancelled(RuntimeError):
    pass


# The provider never has a canonical/hash fallback.  When the public SDK or
# one of its locked dependencies is unavailable, every operation requiring a
# contract identity fails closed with SDK_UNAVAILABLE.
try:
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _sdk_canonical_bytes
    from plotpilot_plugin_sdk.canonical import hash_jcs as _sdk_hash_jcs
    from plotpilot_plugin_sdk.verifier import validate_rpc_result as _sdk_validate_rpc_result
    from plotpilot_plugin_sdk.verifier import verify_checkpoint as _sdk_verify_checkpoint
    from plotpilot_plugin_sdk.verifier import verify_provenance_receipt as _sdk_verify_receipt
    from plotpilot_plugin_sdk.verifier import verify_result_bundle as _sdk_verify_result
    from plotpilot_plugin_sdk.verifier import verify_stream_prefix as _sdk_verify_stream
except ImportError as exc:  # pragma: no cover - exercised by the SDK-absent subprocess test
    _SDK_IMPORT_ERROR: BaseException | None = exc
    _sdk_canonical_bytes = None  # type: ignore[assignment]
    _sdk_hash_jcs = None  # type: ignore[assignment]
    _sdk_validate_rpc_result = None  # type: ignore[assignment]
    _sdk_verify_checkpoint = None  # type: ignore[assignment]
    _sdk_verify_receipt = None  # type: ignore[assignment]
    _sdk_verify_result = None  # type: ignore[assignment]
    _sdk_verify_stream = None  # type: ignore[assignment]
else:
    _SDK_IMPORT_ERROR = None


_IDENTITY = load_runtime_identity()
PLUGIN_ID = "com.plotpilot.novelagent.provider-gemini"
VERSION = "0.1.0"
GEMINI_CAPABILITY_ID = "model.provider.gemini.invoke/v1"
INPUT_SCHEMA = "model.provider.gemini.invoke-request/v1"
OUTPUT_SCHEMA = "model.provider.gemini.invoke-result/v1"
RESULT_CONTRACT = "artifact-bundle/v1"
PACKAGE_HASH = str(_IDENTITY["package_hash"])
RELEASE_ID = str(_IDENTITY["release_id"])
NEEDS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.stream.commit/v1",
)
CORE_ENDPOINT_ORIGIN = "https://generativelanguage.googleapis.com"
CORE_ENDPOINT_PATH_PREFIX = "/v1beta/models/"
_FIXED_TIME = "2026-08-27T00:00:00Z"

DESCRIPTOR: dict[str, Any] = {
    "schema": "capability-provider/v1",
    "capability_id": GEMINI_CAPABILITY_ID,
    "provider": {"plugin_id": PLUGIN_ID, "release_id": RELEASE_ID},
    "input_schema": INPUT_SCHEMA,
    "output_schema": OUTPUT_SCHEMA,
    "result_contract": RESULT_CONTRACT,
    "supports": ["run", "cancel"],
    "deterministic": False,
    "accepted_data_formats": [],
}


class HostPort(Protocol):
    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        ...


class GeminiTransport(Protocol):
    def stream(self, wire_request: Mapping[str, object]) -> Iterable[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class StreamChunk:
    seq: int
    delta: str
    prefix: str
    prefix_hash: str
    usage: Mapping[str, int] | None = None
    prefix_asset_id: str | None = None
    stream_prefix: Mapping[str, Any] | None = None


@dataclass
class _RunContext:
    request_hash: str
    stream_id: str
    receipt_id: str
    checkpoint_ids: tuple[str, ...]
    last_job_event_seq: int = 0
    last_checkpoint_seq: int = 0
    last_stream_seq: int = 0
    previous_prefix: Mapping[str, Any] | None = None


def _require_sdk() -> None:
    if _SDK_IMPORT_ERROR is not None:
        raise ProviderError("SDK_UNAVAILABLE", "public PlotPilot SDK is unavailable; provider is fail-closed") from _SDK_IMPORT_ERROR


def _canonical_bytes(value: Any) -> bytes:
    _require_sdk()
    assert _sdk_canonical_bytes is not None
    return bytes(_sdk_canonical_bytes(value))


def _hash_jcs(prefix: str, value: Any) -> str:
    _require_sdk()
    assert _sdk_hash_jcs is not None
    return str(_sdk_hash_jcs(prefix, value))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _id(prefix: str, request_hash: str, suffix: str = "") -> str:
    tail = request_hash[:20]
    return f"{prefix}-{tail}{('-' + suffix) if suffix else ''}"


def _validate_provider_schema(schema_id: str, value: Any, *, code: str) -> None:
    try:
        validate_schema(schema_id, value)
    except ProviderSchemaError as exc:
        raise ProviderError(code, str(exc)) from exc


def _output_dict(
    status: str,
    result: Mapping[str, Any] | None,
    conditional_result: Mapping[str, Any] | None,
    receipt: Mapping[str, Any],
    error: Mapping[str, Any] | None,
    chunks: tuple[StreamChunk, ...],
    output_text: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "result": result,
        "conditional_result": conditional_result,
        "receipt": receipt,
        "error": error,
        "chunks": [
            {
                "seq": chunk.seq,
                "delta": chunk.delta,
                "prefix": chunk.prefix,
                "prefix_hash": chunk.prefix_hash,
                "usage": dict(chunk.usage) if chunk.usage is not None else None,
                "prefix_asset_id": chunk.prefix_asset_id,
                "stream_prefix": dict(chunk.stream_prefix) if chunk.stream_prefix is not None else None,
            }
            for chunk in chunks
        ],
        "output_text": output_text,
    }


def _validate_output(envelope: Mapping[str, Any]) -> None:
    _require_sdk()
    _validate_provider_schema(OUTPUT_SCHEMA, envelope, code="OUTPUT_SCHEMA_INVALID")
    result = envelope["result"]
    conditional = envelope["conditional_result"]
    if result is not None:
        assert _sdk_verify_result is not None
        _sdk_verify_result(result)
    if conditional is not None:
        assert _sdk_verify_result is not None
        _sdk_verify_result(conditional)
    assert _sdk_verify_receipt is not None
    _sdk_verify_receipt(envelope["receipt"])


@dataclass(frozen=True)
class InvocationOutcome:
    """The closed provider output envelope, including durable stream chunks."""

    status: str
    result: Mapping[str, Any] | None
    conditional_result: Mapping[str, Any] | None
    receipt: Mapping[str, Any]
    error: Mapping[str, Any] | None
    chunks: tuple[StreamChunk, ...] = ()
    output_text: str = ""

    def __post_init__(self) -> None:
        _validate_output(_output_dict(self.status, self.result, self.conditional_result, self.receipt, self.error, self.chunks, self.output_text))

    @property
    def bundle(self) -> Mapping[str, Any] | None:
        return self.result if self.result is not None else self.conditional_result

    def as_dict(self) -> dict[str, Any]:
        value = _output_dict(self.status, self.result, self.conditional_result, self.receipt, self.error, self.chunks, self.output_text)
        _validate_output(value)
        return value


def capability_descriptor() -> dict[str, Any]:
    descriptor = deepcopy(DESCRIPTOR)
    try:
        validate_descriptor_schema_references(descriptor)
    except ProviderSchemaError as exc:
        raise ProviderError("SCHEMA_INDEX_INVALID", str(exc)) from exc
    return descriptor


def _host_call(host: HostPort, method: str, params: Mapping[str, object]) -> dict[str, object]:
    _require_sdk()
    try:
        result = host.call(method, dict(params))
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("HOST_RPC_ERROR", f"{method}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(result, Mapping):
        raise ProviderError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    assert _sdk_validate_rpc_result is not None
    try:
        _sdk_validate_rpc_result(method, result)
    except Exception as exc:
        raise ProviderError("HOST_CONTRACT_ERROR", f"{method} result failed the public RPC schema: {exc}") from exc
    return dict(result)


def _load_request(request: Mapping[str, Any], host: HostPort) -> dict[str, Any]:
    if "request_asset_id" not in request:
        return dict(request)
    if set(request) != {"request_asset_id"}:
        raise ProviderError("INPUT_INVALID", "request_asset_id envelope cannot carry caller fields")
    response = _host_call(
        host,
        "host.asset.read/v1",
        {"asset_id": request["request_asset_id"], "offset": 0, "length": 8_388_608},
    )
    encoded = response.get("base64_chunk")
    if not isinstance(encoded, str):
        raise ProviderError("ASSET_READ_ERROR", "request Asset did not return base64 data")
    try:
        data = base64.b64decode(encoded, validate=True)
        _require_sdk()
        from plotpilot_plugin_sdk.canonical import parse_json_bytes

        loaded = parse_json_bytes(data)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("ASSET_READ_ERROR", "request Asset is not strict UTF-8 JSON") from exc
    if not isinstance(loaded, dict):
        raise ProviderError("INPUT_INVALID", "request Asset must contain a JSON object")
    if response.get("next_offset") != len(data) or response.get("content_hash") != _sha256(data):
        raise ProviderError("ASSET_READ_ERROR", "request Asset read identity is inconsistent")
    return loaded


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ProviderError("INPUT_INVALID", "request must be an object")
    value = dict(request)
    forbidden = {
        "api_key", "access_token", "secret", "credentials", "base_url", "endpoint",
        "transport", "transport_config", "headers", "url", "proxy", "verify_tls",
    }
    leaked = sorted(forbidden.intersection(value))
    if leaked:
        raise ProviderError("INPUT_INVALID", "caller transport/credential fields are forbidden: " + ",".join(leaked))
    if "messages" not in value and isinstance(value.get("prompt"), str):
        value["messages"] = [{"role": "user", "content": value["prompt"]}]
    value.setdefault("max_tokens", 1024)
    value.setdefault("temperature", 0.0)
    value.setdefault("stream", True)
    value.setdefault("output_role", "assistant.text")
    value.setdefault("target", {"workspace_id": "provider-fixture", "entity_kind": "document", "entity_id": "model-output"})
    value.setdefault("created_at", _FIXED_TIME)
    _validate_provider_schema(INPUT_SCHEMA, value, code="INPUT_SCHEMA_INVALID")
    temperature = value["temperature"]
    if isinstance(temperature, bool) or not isinstance(temperature, Real) or not math.isfinite(float(temperature)):
        raise ProviderError("INPUT_INVALID", "temperature must be a finite JSON number")
    checkpoint_ids = value["checkpoint_ids"]
    if len(set(checkpoint_ids)) != len(checkpoint_ids):
        raise ProviderError("INPUT_INVALID", "checkpoint_ids must be unique Core-issued identities")
    return value


def _request_hash(request: Mapping[str, Any]) -> str:
    return _hash_jcs("provider-invocation/v1", dict(request))


def _safe_message(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return message[:240] or type(exc).__name__


def _validate_core_url(url: str, *, model: str | None = None) -> None:
    parsed = urlsplit(url)
    expected = urlsplit(CORE_ENDPOINT_ORIGIN)
    expected_path = (
        f"{CORE_ENDPOINT_PATH_PREFIX}{model}:streamGenerateContent"
        if model is not None
        else None
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query != "alt=sse"
        or parsed.fragment
        or (expected_path is not None and parsed.path != expected_path)
        or (expected_path is None and (not parsed.path.startswith(CORE_ENDPOINT_PATH_PREFIX) or not parsed.path.endswith(":streamGenerateContent")))
        or parsed.port not in (None, 443)
    ):
        raise ValueError("provider endpoint is not the fixed Core-owned origin")


class GeminiHTTPTransport:
    """Stdlib transport whose credential-bearing request is origin-bound."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 300.0) -> None:
        if not isinstance(base_url, str) or not base_url or not isinstance(api_key, str) or not api_key:
            raise ValueError("Core-owned base_url and api_key are required")
        candidate = base_url.rstrip("/") or base_url
        parsed = urlsplit(candidate)
        expected = urlsplit(CORE_ENDPOINT_ORIGIN)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.scheme != "https" or parsed.hostname != expected.hostname or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError("base_url must be the fixed Core-owned origin")
        self.base_url = CORE_ENDPOINT_ORIGIN
        self.api_key = api_key
        self.timeout = timeout

    def stream(self, wire_request: Mapping[str, object]) -> Iterator[Mapping[str, Any]]:
        url = str(wire_request.get("url", ""))
        _validate_core_url(url)
        headers = dict(wire_request.get("headers") or {})  # type: ignore[arg-type]
        headers["x-goog-api-key"] = self.api_key
        payload = json.dumps(wire_request.get("body"), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        req = urllib_request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:  # nosec B310 - fixed provider origin above
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data:
                        parsed = json.loads(data)
                        if isinstance(parsed, Mapping):
                            yield dict(parsed)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("UPSTREAM_TRANSPORT_ERROR", _safe_message(exc), retryable=True) from exc


class GeminiProvider:
    plugin_id = PLUGIN_ID
    capability_id = GEMINI_CAPABILITY_ID
    release_id = RELEASE_ID
    package_hash = PACKAGE_HASH

    def __init__(self, transport: GeminiTransport | None = None) -> None:
        self.transport = transport
        self._cancelled: set[str] = set()

    def descriptor(self) -> dict[str, Any]:
        return capability_descriptor()

    def cancel(self, run_id_or_request: str | Mapping[str, Any]) -> bool:
        run_id = run_id_or_request if isinstance(run_id_or_request, str) else str(run_id_or_request.get("worker_run_id") or run_id_or_request.get("invocation_id") or "")
        if not run_id:
            return False
        was_new = run_id not in self._cancelled
        self._cancelled.add(run_id)
        return was_new

    def _wire_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system: list[str] = []
        for item in request["messages"]:
            if item["role"] == "system":
                system.append(item["content"])
            else:
                contents.append({"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]})
        generation_config: dict[str, Any] = {"temperature": request["temperature"], "maxOutputTokens": request["max_tokens"]}
        response_format = request.get("response_format")
        if isinstance(response_format, Mapping) and response_format.get("type") in {"json", "json_object", "json_schema"}:
            generation_config["responseMimeType"] = "application/json"
        body: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        url = f"{CORE_ENDPOINT_ORIGIN}{CORE_ENDPOINT_PATH_PREFIX}{request['model']}:streamGenerateContent?alt=sse"
        return {"url": url, "headers": {"content-type": "application/json", "accept": "text/event-stream"}, "body": body}

    @staticmethod
    def _accepted(result: Mapping[str, object], method: str) -> None:
        if result.get("accepted") is not True:
            raise ProviderError("HOST_REJECTED", f"Host rejected {method}")

    def _record_event(self, context: _RunContext, result: Mapping[str, object], method: str) -> None:
        self._accepted(result, method)
        seq = result.get("job_event_seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < context.last_job_event_seq:
            raise ProviderError("HOST_CONTRACT_ERROR", f"{method} job_event_seq moved backwards")
        context.last_job_event_seq = seq

    def _upload(self, host: HostPort, context: _RunContext, data: bytes, mime: str, suffix: str) -> str:
        expected_hash = _sha256(data)
        upload_id = f"{context.request_hash}-upload-{suffix}"
        created = _host_call(host, "host.asset.create/v1", {"operation_key": context.request_hash, "upload_id": upload_id, "offset": 0, "mime": mime, "total_size": len(data), "expected_hash": expected_hash, "chunk_hash": expected_hash, "base64_chunk": base64.b64encode(data).decode("ascii"), "final": True})
        asset_id = created.get("asset_id")
        if created.get("upload_id") != upload_id or created.get("accepted_bytes") != len(data) or created.get("completed") is not True or not isinstance(asset_id, str) or not asset_id:
            raise ProviderError("ASSET_CREATE_ERROR", "Host Asset create identity/size was not accepted")
        status = _host_call(host, "host.asset.upload.status/v1", {"upload_id": upload_id, "expected_hash": expected_hash})
        if status.get("accepted_bytes") != len(data) or status.get("completed") is not True or status.get("asset_id") != asset_id:
            raise ProviderError("ASSET_UPLOAD_ERROR", "Host Asset upload status did not match the upload")
        return asset_id

    def _event(self, host: HostPort, context: _RunContext, event_type: str, payload_asset_id: str | None, seq: int) -> None:
        result = _host_call(host, "host.job.event/v1", {"operation_key": context.request_hash, "event_type": event_type, "payload_asset_id": payload_asset_id, "local_seq": seq})
        self._record_event(context, result, "host.job.event/v1")

    def _checkpoint(self, host: HostPort, request: Mapping[str, Any], context: _RunContext, prefix_asset_id: str, seq: int) -> Mapping[str, Any]:
        if seq > len(context.checkpoint_ids):
            raise ProviderError("CHECKPOINT_ID_EXHAUSTED", "Core did not issue a checkpoint identity for this stream sequence")
        checkpoint_id = context.checkpoint_ids[seq - 1]
        checkpoint: dict[str, Any] = {"schema": "checkpoint/v1", "checkpoint_id": checkpoint_id, "checkpoint_seq": seq, "job_id": request["job_id"], "step_id": request["step_id"], "source_attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "replay_policy": "checkpoint_resume", "completed_units": seq, "total_units": request.get("total_units") if isinstance(request.get("total_units"), int) else None, "unit_set_hash": None, "state_asset_id": prefix_asset_id, "created_at": request.get("created_at", _FIXED_TIME)}
        checkpoint["checkpoint_hash"] = _hash_jcs("checkpoint/v1", checkpoint)
        assert _sdk_verify_checkpoint is not None
        _sdk_verify_checkpoint(checkpoint, expected_snapshot_hash=str(request["run_snapshot_hash"]), previous_seq=context.last_checkpoint_seq or None)
        checkpoint_asset_id = self._upload(host, context, _canonical_bytes(checkpoint), "application/json", f"checkpoint-{seq}")
        response = _host_call(host, "host.checkpoint.commit/v1", {"operation_key": context.request_hash, "checkpoint_asset_id": checkpoint_asset_id})
        self._accepted(response, "host.checkpoint.commit/v1")
        if response.get("checkpoint_id") != checkpoint_id or response.get("completed_units") != seq or response.get("total_units") != checkpoint["total_units"]:
            raise ProviderError("CHECKPOINT_ID_MISMATCH", "Host checkpoint result does not match the Core-issued identity")
        self._record_event(context, response, "host.checkpoint.commit/v1")
        context.last_checkpoint_seq = seq
        return checkpoint

    def _receipt(self, request: Mapping[str, Any], receipt_id: str, bundle_id: str | None, bundle_hash: str | None) -> dict[str, Any]:
        receipt: dict[str, Any] = {"schema": "provenance-receipt/v1", "receipt_id": receipt_id, "plugin_id": PLUGIN_ID, "release_id": RELEASE_ID, "package_hash": PACKAGE_HASH, "capability_id": GEMINI_CAPABILITY_ID, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"], "run_snapshot_hash": request["run_snapshot_hash"], "bundle_id": bundle_id, "bundle_hash": bundle_hash, "parent_receipt_ids": [], "model_receipt_ids": [], "skill_chain_result_refs": [], "staged_items": [], "created_at": request.get("created_at", _FIXED_TIME)}
        receipt["receipt_hash"] = _hash_jcs("provenance-receipt/v1", receipt)
        assert _sdk_verify_receipt is not None
        _sdk_verify_receipt(receipt)
        return receipt

    def _complete_host(self, host: HostPort, request: Mapping[str, Any], context: _RunContext, outcome: str, result_asset_id: str | None, detail_asset_id: str | None, local_seq: int) -> None:
        response = _host_call(host, "host.job.complete/v1", {"operation_key": context.request_hash, "worker_run_id": request["worker_run_id"], "outcome": outcome, "result_bundle_asset_id": result_asset_id, "candidate_stage_operation_key": None, "terminal_detail_asset_id": detail_asset_id, "local_seq": local_seq})
        self._accepted(response, "host.job.complete/v1")
        expected_receipt = request["provenance_receipt_id"]
        if response.get("provenance_receipt_id") != expected_receipt or response.get("attempt_state") != outcome or response.get("step_state") != outcome or response.get("job_state") != outcome:
            raise ProviderError("TERMINAL_IDENTITY_MISMATCH", "Host terminal result did not preserve Core job/attempt/receipt identity")
        if outcome == "succeeded" and not isinstance(result_asset_id, str):
            raise ProviderError("TERMINAL_RESULT_MISSING", "accepted succeeded terminal result has no result Asset")
        event_seq = response.get("job_event_seq")
        high_water = response.get("core_event_high_water")
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < context.last_job_event_seq or isinstance(high_water, bool) or not isinstance(high_water, int) or high_water < event_seq:
            raise ProviderError("HOST_CONTRACT_ERROR", "Host terminal event identity/high-water is invalid")
        context.last_job_event_seq = event_seq

    def _failure(self, host: HostPort, request: Mapping[str, Any], context: _RunContext, error: ProviderError, chunks: tuple[StreamChunk, ...], output_text: str) -> InvocationOutcome:
        error_payload = {"code": error.code, "message": _safe_message(error), "retryable": error.retryable}
        receipt_id = request["provenance_receipt_id"]
        try:
            detail_bytes = _canonical_bytes(error_payload)
            detail_asset_id = self._upload(host, context, detail_bytes, "application/json", "failure-detail")
            diagnostic: dict[str, Any] = {"schema": "result-bundle/v1", "contract_id": "diagnostic-bundle/v1", "bundle_id": _id("gemini-failure", context.request_hash), "bundle_type": "diagnostic", "producer": {"plugin_id": PLUGIN_ID, "release_id": RELEASE_ID, "capability_id": GEMINI_CAPABILITY_ID, "job_id": request["job_id"], "step_id": request["step_id"], "attempt_id": request["attempt_id"], "lease_epoch": request["lease_epoch"]}, "input_snapshot_hash": request["run_snapshot_hash"], "items": [{"schema": "diagnostic-item/v1", "item_id": _id("gemini-diagnostic", context.request_hash), "severity": "error", "code": error.code, "message": _safe_message(error), "details_asset_id": detail_asset_id, "details_hash": _sha256(detail_bytes), "source_refs": [], "status": "failed"}], "warnings": [], "partial": True, "provenance_receipt_id": receipt_id, "skill_chain_result_refs": []}
            assert _sdk_verify_result is not None
            _sdk_verify_result(diagnostic, snapshot_hash_value=str(request["run_snapshot_hash"]))
            bundle_asset_id = self._upload(host, context, _canonical_bytes(diagnostic), "application/json", "failure-bundle")
            receipt = self._receipt(request, receipt_id, diagnostic["bundle_id"], _hash_jcs("result-bundle/v1", diagnostic))
            self._complete_host(host, request, context, "failed", bundle_asset_id, detail_asset_id, len(chunks) + 1)
            return InvocationOutcome("failed", None, diagnostic, receipt, error_payload, chunks, output_text)
        except Exception as host_error:
            receipt = self._receipt(request, receipt_id, None, None)
            try:
                self._complete_host(host, request, context, "failed", None, None, len(chunks) + 1)
            except Exception:
                pass
            fallback_error = {"code": "HOST_CONTRACT_ERROR", "message": f"{error.code}; {_safe_message(host_error)}", "retryable": False}
            return InvocationOutcome("failed", None, None, receipt, fallback_error, chunks, output_text)

    def _cancelled_outcome(self, host: HostPort, request: Mapping[str, Any], context: _RunContext, chunks: tuple[StreamChunk, ...], output_text: str) -> InvocationOutcome:
        receipt = self._receipt(request, context.receipt_id, None, None)
        error = {"code": "CANCELLED", "message": "provider run cancelled", "retryable": False}
        self._complete_host(host, request, context, "cancelled", None, None, len(chunks) + 1)
        return InvocationOutcome("cancelled", None, None, receipt, error, chunks, output_text)

    def run(self, request: Mapping[str, Any], host: HostPort, *, transport: GeminiTransport | None = None) -> InvocationOutcome:
        _require_sdk()
        normalized = _validate_request(_load_request(request, host))
        request_hash = _request_hash(normalized)
        run_id = normalized["worker_run_id"]
        context = _RunContext(request_hash, normalized["stream_id"], normalized["provenance_receipt_id"], tuple(normalized["checkpoint_ids"]))
        if run_id in self._cancelled:
            return self._cancelled_outcome(host, normalized, context, (), "")
        active_transport = transport or self.transport
        if active_transport is None:
            raise ValueError("an GeminiTransport is required; no network transport is implicit")
        self._event(host, context, "provider.started", None, 1)
        chunks: list[StreamChunk] = []
        prefix = ""
        usage: dict[str, int] = {}
        try:
            for event in active_transport.stream(self._wire_request(normalized)):
                if run_id in self._cancelled:
                    raise _Cancelled()
                raw_error = event.get("error")
                if isinstance(raw_error, Mapping):
                    raise ProviderError("UPSTREAM_ERROR", str(raw_error.get("message") or raw_error.get("status") or "Gemini stream error"))
                feedback = event.get("promptFeedback")
                if isinstance(feedback, Mapping) and feedback.get("blockReason"):
                    raise ProviderError("PROMPT_BLOCKED", str(feedback["blockReason"]))
                meta = event.get("usageMetadata")
                if isinstance(meta, Mapping):
                    usage["input_tokens"] = int(meta.get("promptTokenCount") or usage.get("input_tokens", 0))
                    usage["output_tokens"] = int(meta.get("candidatesTokenCount") or usage.get("output_tokens", 0))
                candidates = event.get("candidates") or []
                if not isinstance(candidates, list):
                    raise ProviderError("UPSTREAM_ERROR", "Gemini candidates must be an array")
                for candidate in candidates:
                    if not isinstance(candidate, Mapping):
                        raise ProviderError("UPSTREAM_ERROR", "Gemini candidate must be an object")
                    content = candidate.get("content") or {}
                    parts = content.get("parts") if isinstance(content, Mapping) else None
                    if parts is None:
                        continue
                    if not isinstance(parts, list):
                        raise ProviderError("UPSTREAM_ERROR", "Gemini content parts must be an array")
                    for part in parts:
                        text = part.get("text") if isinstance(part, Mapping) else None
                        if not isinstance(text, str) or not text:
                            continue
                        prefix += text
                        seq = len(chunks) + 1
                        prefix_bytes = prefix.encode("utf-8")
                        prefix_hash = _sha256(prefix_bytes)
                        prefix_asset_id = self._upload(host, context, prefix_bytes, "text/plain; charset=utf-8", f"stream-{seq}")
                        stream_prefix = {"schema": "stream-prefix/v1", "stream_id": context.stream_id, "job_id": normalized["job_id"], "step_id": normalized["step_id"], "output_role": normalized["output_role"], "target": dict(normalized["target"]), "attempt_id": normalized["attempt_id"], "lease_epoch": normalized["lease_epoch"], "prefix_seq": seq, "prefix_asset_id": prefix_asset_id, "prefix_hash": prefix_hash, "byte_length": len(prefix_bytes), "encoding": "utf-8"}
                        assert _sdk_verify_stream is not None
                        _sdk_verify_stream(stream_prefix, previous=context.previous_prefix, content=prefix_bytes)
                        ack = _host_call(host, "host.stream.commit/v1", {"operation_key": context.request_hash, "stream_prefix_asset_id": prefix_asset_id})
                        self._accepted(ack, "host.stream.commit/v1")
                        if ack.get("stream_id") != context.stream_id or ack.get("acked_prefix_seq") != seq or ack.get("acked_bytes") != len(prefix_bytes) or ack.get("acked_prefix_hash") != prefix_hash:
                            raise ProviderError("STREAM_IDENTITY_MISMATCH", "Host stream ACK did not match the Core-issued stream/prefix")
                        self._record_event(context, ack, "host.stream.commit/v1")
                        context.last_stream_seq = seq
                        context.previous_prefix = stream_prefix
                        chunks.append(StreamChunk(seq, text, prefix, prefix_hash, dict(usage) if usage else None, prefix_asset_id, stream_prefix))
                        self._event(host, context, "provider.chunk", prefix_asset_id, seq + 1)
                        self._checkpoint(host, normalized, context, prefix_asset_id, seq)
            if not prefix:
                raise ProviderError("EMPTY_MODEL_RESPONSE", "Gemini returned no text")
            output_bytes = prefix.encode("utf-8")
            output_asset_id = self._upload(host, context, output_bytes, "text/plain; charset=utf-8", "output")
            receipt_id = context.receipt_id
            bundle: dict[str, Any] = {"schema": "result-bundle/v1", "contract_id": RESULT_CONTRACT, "bundle_id": _id("gemini-bundle", context.request_hash), "bundle_type": "artifact", "producer": {"plugin_id": PLUGIN_ID, "release_id": RELEASE_ID, "capability_id": GEMINI_CAPABILITY_ID, "job_id": normalized["job_id"], "step_id": normalized["step_id"], "attempt_id": normalized["attempt_id"], "lease_epoch": normalized["lease_epoch"]}, "input_snapshot_hash": normalized["run_snapshot_hash"], "items": [{"schema": "artifact-item/v1", "item_id": _id("gemini-artifact", context.request_hash), "artifact_kind": "model-response", "payload_asset_id": output_asset_id, "payload_hash": _sha256(output_bytes), "mime": normalized.get("response_mime", "text/plain; charset=utf-8"), "source_refs": [], "status": "complete"}], "warnings": [], "partial": False, "provenance_receipt_id": receipt_id, "skill_chain_result_refs": []}
            assert _sdk_verify_result is not None
            _sdk_verify_result(bundle, snapshot_hash_value=str(normalized["run_snapshot_hash"]))
            bundle_asset_id = self._upload(host, context, _canonical_bytes(bundle), "application/json", "result")
            receipt = self._receipt(normalized, receipt_id, bundle["bundle_id"], _hash_jcs("result-bundle/v1", bundle))
            self._complete_host(host, normalized, context, "succeeded", bundle_asset_id, None, len(chunks) + 1)
            return InvocationOutcome("succeeded", bundle, None, receipt, None, tuple(chunks), prefix)
        except _Cancelled:
            return self._cancelled_outcome(host, normalized, context, tuple(chunks), prefix)
        except ProviderError as exc:
            return self._failure(host, normalized, context, exc, tuple(chunks), prefix)
        except Exception as exc:
            return self._failure(host, normalized, context, ProviderError("UPSTREAM_FAILURE", f"{type(exc).__name__}: {_safe_message(exc)}", retryable=True), tuple(chunks), prefix)


def main() -> dict[str, Any]:
    return capability_descriptor()
