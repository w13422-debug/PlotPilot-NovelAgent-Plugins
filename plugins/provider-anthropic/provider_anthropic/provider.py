"""Anthropic Messages wire adapter and B0 provider contract boundary.

The module deliberately has no dependency on another provider.  It consumes
the public PlotPilot SDK when that package is available and keeps only the
canonical hashing primitives needed to run the package in this design-only
worktree when the SDK has not yet been published here.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from numbers import Real
from typing import Any, Iterable, Iterator, Mapping, Protocol
from urllib import request as urllib_request

try:  # Public SDK is the authoritative implementation when installed.
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes
    from plotpilot_plugin_sdk.canonical import hash_jcs as _hash_jcs
    from plotpilot_plugin_sdk.contracts import ResultBundle as PublicResultBundle
except ImportError:  # B0 worktree fallback: IDs, not a second JSON schema.
    PublicResultBundle = dict  # type: ignore[misc,assignment]

    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def _hash_jcs(prefix: str, value: Any) -> str:
        return hashlib.sha256(
            prefix.encode("ascii") + b"\n" + _canonical_bytes(value)
        ).hexdigest()


ResultBundle = PublicResultBundle

PLUGIN_ID = "com.plotpilot.novelagent.provider-anthropic"
ANTHROPIC_CAPABILITY_ID = "model.provider.anthropic.invoke/v1"
INPUT_SCHEMA = "model.provider.anthropic.invoke-request/v1"
OUTPUT_SCHEMA = "model.provider.anthropic.invoke-result/v1"
RESULT_CONTRACT = "artifact-bundle/v1"
PACKAGE_HASH = hashlib.sha256(
    b"plotpilot-provider-package/v1\n" + PLUGIN_ID.encode("ascii")
).hexdigest()
RELEASE_ID = hashlib.sha256(
    b"plotpilot-provider-release/v1\n"
    + PLUGIN_ID.encode("ascii")
    + b"\n0.1.0\n"
    + PACKAGE_HASH.encode("ascii")
    + b"\n"
).hexdigest()
NEEDS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.stream.commit/v1",
)
DESCRIPTOR: dict[str, Any] = {
    "schema": "capability-provider/v1",
    "capability_id": ANTHROPIC_CAPABILITY_ID,
    "provider": {"plugin_id": PLUGIN_ID, "release_id": RELEASE_ID},
    "input_schema": INPUT_SCHEMA,
    "output_schema": OUTPUT_SCHEMA,
    "result_contract": RESULT_CONTRACT,
    "supports": ["run", "cancel"],
    "deterministic": False,
    "accepted_data_formats": [],
}

_FIXED_TIME = "2026-08-27T00:00:00Z"


class HostPort(Protocol):
    """The only host surface used by this provider."""

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        ...


class AnthropicTransport(Protocol):
    def stream(self, wire_request: Mapping[str, object]) -> Iterable[Mapping[str, Any]]:
        ...


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class _Cancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamChunk:
    """A normalized delta plus its durable UTF-8 prefix identity."""

    seq: int
    delta: str
    prefix: str
    prefix_hash: str
    usage: Mapping[str, int] | None = None
    prefix_asset_id: str | None = None
    stream_prefix: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class InvocationOutcome:
    """Python control envelope; ``result`` remains the only success contract."""

    status: str
    result: Mapping[str, Any] | None
    conditional_result: Mapping[str, Any] | None
    receipt: Mapping[str, Any]
    error: Mapping[str, Any] | None
    chunks: tuple[StreamChunk, ...] = ()
    output_text: str = ""

    @property
    def bundle(self) -> Mapping[str, Any] | None:
        return self.result if self.result is not None else self.conditional_result

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "result": self.result,
            "conditional_result": self.conditional_result,
            "receipt": self.receipt,
            "error": self.error,
        }


def capability_descriptor() -> dict[str, Any]:
    return deepcopy(DESCRIPTOR)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _without(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    return result


def _id(prefix: str, request_hash: str, suffix: str = "") -> str:
    tail = request_hash[:20]
    return f"{prefix}-{tail}{('-' + suffix) if suffix else ''}"


def _host_call(host: HostPort, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
    result = host.call(method, dict(params))
    if not isinstance(result, Mapping):
        raise ProviderError("HOST_CONTRACT_ERROR", f"{method} returned a non-object")
    return result


def _load_request(request: Mapping[str, Any], host: HostPort) -> dict[str, Any]:
    if "request_asset_id" not in request:
        return dict(request)
    response = _host_call(
        host,
        "host.asset.read/v1",
        {"asset_id": request["request_asset_id"], "offset": 0, "length": 8_388_608},
    )
    encoded = response.get("base64_chunk")
    if not isinstance(encoded, str):
        raise ProviderError("ASSET_READ_ERROR", "request Asset did not return base64 data")
    try:
        loaded = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("ASSET_READ_ERROR", "request Asset is not UTF-8 JSON") from exc
    if not isinstance(loaded, dict):
        raise ProviderError("INPUT_INVALID", "request Asset must contain a JSON object")
    return loaded


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(request)
    if value.get("schema") != INPUT_SCHEMA:
        raise ProviderError("INPUT_INVALID", f"schema must be {INPUT_SCHEMA}")
    forbidden = {"api_key", "access_token", "secret", "credentials"}
    leaked = sorted(forbidden.intersection(value))
    if leaked:
        raise ProviderError("INPUT_INVALID", "credentials must not be supplied in the request")
    for field in ("invocation_id", "job_id", "step_id", "attempt_id", "model"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ProviderError("INPUT_INVALID", f"{field} is required")
    lease_epoch = value.get("lease_epoch")
    if isinstance(lease_epoch, bool) or not isinstance(lease_epoch, int) or lease_epoch < 1:
        raise ProviderError("INPUT_INVALID", "lease_epoch must be a positive integer")
    snapshot_hash = value.get("run_snapshot_hash")
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64 or any(
        char not in "0123456789abcdef" for char in snapshot_hash
    ):
        raise ProviderError("INPUT_INVALID", "run_snapshot_hash must be lowercase SHA-256")
    max_tokens = value.get("max_tokens", 1024)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ProviderError("INPUT_INVALID", "max_tokens must be a positive integer")
    temperature = value.get("temperature", 0.0)
    if isinstance(temperature, bool) or not isinstance(temperature, Real):
        raise ProviderError("INPUT_INVALID", "temperature must be numeric")
    messages = value.get("messages")
    if messages is None and isinstance(value.get("prompt"), str):
        messages = [{"role": "user", "content": value["prompt"]}]
        value["messages"] = messages
    if not isinstance(messages, list) or not messages:
        raise ProviderError("INPUT_INVALID", "messages must be a non-empty list")
    for item in messages:
        if not isinstance(item, Mapping) or item.get("role") not in {"system", "user", "assistant"}:
            raise ProviderError("INPUT_INVALID", "message role is invalid")
        if not isinstance(item.get("content"), str):
            raise ProviderError("INPUT_INVALID", "message content must be text")
    value["max_tokens"] = max_tokens
    value["temperature"] = temperature
    value.setdefault("stream", True)
    value.setdefault("output_role", "assistant.text")
    value.setdefault(
        "target",
        {"workspace_id": "provider-fixture", "entity_kind": "document", "entity_id": "model-output"},
    )
    if not isinstance(value["target"], Mapping):
        raise ProviderError("INPUT_INVALID", "target must be an object")
    target = value["target"]
    if not all(isinstance(target.get(field), str) and target[field] for field in ("workspace_id", "entity_kind", "entity_id")):
        raise ProviderError("INPUT_INVALID", "target identity is incomplete")
    value.setdefault("created_at", _FIXED_TIME)
    return value


def _request_hash(request: Mapping[str, Any]) -> str:
    return _hash_jcs("provider-invocation/v1", dict(request))


def _safe_message(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return message[:240] or type(exc).__name__


class AnthropicHTTPTransport:
    """Optional stdlib transport; tests use ScriptedTransport instead."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 300.0) -> None:
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def stream(self, wire_request: Mapping[str, object]) -> Iterator[Mapping[str, Any]]:
        url = str(wire_request["url"])
        headers = dict(wire_request["headers"])  # type: ignore[arg-type]
        headers["x-api-key"] = self.api_key
        payload = json.dumps(wire_request["body"], ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:  # nosec B310 - explicit provider endpoint
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data:
                        parsed = json.loads(data)
                        if isinstance(parsed, Mapping):
                            yield dict(parsed)
        except Exception as exc:
            raise ProviderError("UPSTREAM_TRANSPORT_ERROR", _safe_message(exc), retryable=True) from exc


class AnthropicProvider:
    """Headless adapter. Core remains the owner of Job/Asset/stream identity."""

    plugin_id = PLUGIN_ID
    capability_id = ANTHROPIC_CAPABILITY_ID
    release_id = RELEASE_ID
    package_hash = PACKAGE_HASH

    def __init__(self, transport: AnthropicTransport | None = None) -> None:
        self.transport = transport
        self._cancelled: set[str] = set()

    def descriptor(self) -> dict[str, Any]:
        return capability_descriptor()

    def cancel(self, run_id_or_request: str | Mapping[str, Any]) -> bool:
        run_id = (
            run_id_or_request
            if isinstance(run_id_or_request, str)
            else str(run_id_or_request.get("worker_run_id") or run_id_or_request.get("invocation_id") or "")
        )
        if not run_id:
            return False
        was_new = run_id not in self._cancelled
        self._cancelled.add(run_id)
        return was_new

    def _wire_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        system = [item["content"] for item in request["messages"] if item["role"] == "system"]
        messages = [
            {"role": item["role"], "content": item["content"]}
            for item in request["messages"]
            if item["role"] in {"user", "assistant"}
        ]
        body: dict[str, Any] = {
            "model": request["model"],
            "messages": messages,
            "max_tokens": request["max_tokens"],
            "temperature": request["temperature"],
            "stream": True,
        }
        if system:
            body["system"] = "\n\n".join(system)
        return {
            "url": f"{request.get('base_url', 'https://api.anthropic.com').rstrip('/')}/v1/messages",
            "headers": {
                "content-type": "application/json",
                "accept": "text/event-stream",
                "anthropic-version": "2023-06-01",
            },
            "body": body,
        }

    def _upload(self, host: HostPort, operation_key: str, data: bytes, mime: str, suffix: str) -> str:
        expected_hash = _sha256(data)
        upload_id = f"{operation_key}-upload-{suffix}"
        created = _host_call(
            host,
            "host.asset.create/v1",
            {
                "operation_key": operation_key,
                "upload_id": upload_id,
                "offset": 0,
                "mime": mime,
                "total_size": len(data),
                "expected_hash": expected_hash,
                "chunk_hash": expected_hash,
                "base64_chunk": base64.b64encode(data).decode("ascii"),
                "final": True,
            },
        )
        asset_id = created.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ProviderError("ASSET_CREATE_ERROR", "Host did not return an Asset ID")
        status = _host_call(
            host,
            "host.asset.upload.status/v1",
            {"upload_id": upload_id, "expected_hash": expected_hash},
        )
        if status.get("completed") is not True or status.get("asset_id") != asset_id:
            raise ProviderError("ASSET_UPLOAD_ERROR", "Host did not confirm the completed Asset")
        return asset_id

    def _event(self, host: HostPort, op: str, event_type: str, payload_asset_id: str | None, seq: int) -> None:
        _host_call(
            host,
            "host.job.event/v1",
            {"operation_key": op, "event_type": event_type, "payload_asset_id": payload_asset_id, "local_seq": seq},
        )

    def _checkpoint(
        self,
        host: HostPort,
        request: Mapping[str, Any],
        request_hash: str,
        prefix_asset_id: str,
        seq: int,
    ) -> Mapping[str, Any]:
        checkpoint: dict[str, Any] = {
            "schema": "checkpoint/v1",
            "checkpoint_id": _id("anthropic-checkpoint", request_hash, str(seq)),
            "checkpoint_seq": seq,
            "job_id": request["job_id"],
            "step_id": request["step_id"],
            "source_attempt_id": request["attempt_id"],
            "lease_epoch": request["lease_epoch"],
            "run_snapshot_hash": request["run_snapshot_hash"],
            "replay_policy": "checkpoint_resume",
            "completed_units": seq,
            "total_units": request.get("total_units") if isinstance(request.get("total_units"), int) else None,
            "unit_set_hash": None,
            "state_asset_id": prefix_asset_id,
            "created_at": request.get("created_at", _FIXED_TIME),
        }
        checkpoint["checkpoint_hash"] = _hash_jcs("checkpoint/v1", checkpoint)
        data = _canonical_bytes(checkpoint)
        asset_id = self._upload(host, f"{request_hash}-checkpoint", data, "application/json", str(seq))
        response = _host_call(
            host,
            "host.checkpoint.commit/v1",
            {"operation_key": request_hash, "checkpoint_asset_id": asset_id},
        )
        if response.get("accepted") is not True:
            raise ProviderError("CHECKPOINT_REJECTED", "Host rejected checkpoint commit")
        return checkpoint

    def _receipt(
        self,
        request: Mapping[str, Any],
        request_hash: str,
        receipt_id: str,
        bundle_id: str | None,
        bundle_hash: str | None,
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schema": "provenance-receipt/v1",
            "receipt_id": receipt_id,
            "plugin_id": PLUGIN_ID,
            "release_id": RELEASE_ID,
            "package_hash": PACKAGE_HASH,
            "capability_id": ANTHROPIC_CAPABILITY_ID,
            "job_id": request["job_id"],
            "step_id": request["step_id"],
            "attempt_id": request["attempt_id"],
            "lease_epoch": request["lease_epoch"],
            "run_snapshot_hash": request["run_snapshot_hash"],
            "bundle_id": bundle_id,
            "bundle_hash": bundle_hash,
            "parent_receipt_ids": [],
            "model_receipt_ids": [],
            "skill_chain_result_refs": [],
            "staged_items": [],
            "created_at": request.get("created_at", _FIXED_TIME),
        }
        receipt["receipt_hash"] = _hash_jcs("provenance-receipt/v1", receipt)
        return receipt

    def _complete_host(
        self,
        host: HostPort,
        request: Mapping[str, Any],
        operation_key: str,
        outcome: str,
        result_asset_id: str | None,
        detail_asset_id: str | None,
        local_seq: int,
    ) -> None:
        _host_call(
            host,
            "host.job.complete/v1",
            {
                "operation_key": operation_key,
                "worker_run_id": request.get("worker_run_id", request["invocation_id"]),
                "outcome": outcome,
                "result_bundle_asset_id": result_asset_id,
                "candidate_stage_operation_key": None,
                "terminal_detail_asset_id": detail_asset_id,
                "local_seq": local_seq,
            },
        )

    def _failure(
        self,
        host: HostPort,
        request: Mapping[str, Any],
        request_hash: str,
        error: ProviderError,
        chunks: tuple[StreamChunk, ...],
        output_text: str,
    ) -> InvocationOutcome:
        error_payload = {"code": error.code, "message": _safe_message(error), "retryable": error.retryable}
        receipt_id = str(request.get("provenance_receipt_id") or _id("anthropic-receipt", request_hash))
        diagnostic: dict[str, Any] | None = None
        detail_asset_id: str | None = None
        try:
            detail_bytes = _canonical_bytes(error_payload)
            detail_asset_id = self._upload(host, request_hash, detail_bytes, "application/json", "failure-detail")
            detail_hash = _sha256(detail_bytes)
            item_id = _id("anthropic-diagnostic", request_hash)
            diagnostic = {
                "schema": "result-bundle/v1",
                "contract_id": "diagnostic-bundle/v1",
                "bundle_id": _id("anthropic-failure", request_hash),
                "bundle_type": "diagnostic",
                "producer": {
                    "plugin_id": PLUGIN_ID,
                    "release_id": RELEASE_ID,
                    "capability_id": ANTHROPIC_CAPABILITY_ID,
                    "job_id": request["job_id"],
                    "step_id": request["step_id"],
                    "attempt_id": request["attempt_id"],
                    "lease_epoch": request["lease_epoch"],
                },
                "input_snapshot_hash": request["run_snapshot_hash"],
                "items": [{
                    "schema": "diagnostic-item/v1",
                    "item_id": item_id,
                    "severity": "error",
                    "code": error.code,
                    "message": _safe_message(error),
                    "details_asset_id": detail_asset_id,
                    "details_hash": detail_hash,
                    "source_refs": [],
                    "status": "failed",
                }],
                "warnings": [],
                "partial": True,
                "provenance_receipt_id": receipt_id,
                "skill_chain_result_refs": [],
            }
            diagnostic_bytes = _canonical_bytes(diagnostic)
            bundle_asset_id = self._upload(host, request_hash, diagnostic_bytes, "application/json", "failure-bundle")
            bundle_hash = _hash_jcs("result-bundle/v1", diagnostic)
            receipt = self._receipt(request, request_hash, receipt_id, diagnostic["bundle_id"], bundle_hash)
            self._complete_host(host, request, request_hash, "failed", bundle_asset_id, detail_asset_id, len(chunks) + 1)
            return InvocationOutcome("failed", None, diagnostic, receipt, error_payload, chunks, output_text)
        except Exception as host_error:
            receipt = self._receipt(request, request_hash, receipt_id, None, None)
            try:
                self._complete_host(host, request, request_hash, "failed", None, detail_asset_id, len(chunks) + 1)
            except Exception:
                pass
            error_payload = {
                "code": "HOST_CONTRACT_ERROR",
                "message": f"{error.code}; {_safe_message(host_error)}",
                "retryable": False,
            }
            return InvocationOutcome("failed", None, None, receipt, error_payload, chunks, output_text)

    def _cancelled_outcome(
        self, host: HostPort, request: Mapping[str, Any], request_hash: str, chunks: tuple[StreamChunk, ...], output_text: str
    ) -> InvocationOutcome:
        receipt_id = str(request.get("provenance_receipt_id") or _id("anthropic-receipt", request_hash))
        receipt = self._receipt(request, request_hash, receipt_id, None, None)
        error = {"code": "CANCELLED", "message": "provider run cancelled", "retryable": False}
        self._complete_host(host, request, request_hash, "cancelled", None, None, len(chunks) + 1)
        return InvocationOutcome("cancelled", None, None, receipt, error, chunks, output_text)

    def run(self, request: Mapping[str, Any], host: HostPort, *, transport: AnthropicTransport | None = None) -> InvocationOutcome:
        loaded = _load_request(request, host)
        normalized = _validate_request(loaded)
        request_hash = _request_hash(normalized)
        run_id = str(normalized.get("worker_run_id", normalized["invocation_id"]))
        if run_id in self._cancelled:
            return self._cancelled_outcome(host, normalized, request_hash, (), "")
        active_transport = transport or self.transport
        if active_transport is None:
            raise ValueError("an AnthropicTransport is required; no network transport is implicit")
        operation_key = request_hash
        self._event(host, operation_key, "provider.started", None, 1)
        wire = self._wire_request(normalized)
        chunks: list[StreamChunk] = []
        prefix = ""
        usage: dict[str, int] = {}
        try:
            for event in active_transport.stream(wire):
                if run_id in self._cancelled:
                    raise _Cancelled()
                event_type = event.get("type")
                if event_type == "message_start":
                    raw_usage = (event.get("message") or {}).get("usage", {})
                    if isinstance(raw_usage, Mapping):
                        usage["input_tokens"] = int(raw_usage.get("input_tokens") or 0)
                elif event_type == "message_delta":
                    raw_usage = event.get("usage") or {}
                    if isinstance(raw_usage, Mapping):
                        usage["output_tokens"] = int(raw_usage.get("output_tokens") or 0)
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    text = delta.get("text") if isinstance(delta, Mapping) and delta.get("type") == "text_delta" else None
                    if not isinstance(text, str) or not text:
                        continue
                    prefix += text
                    seq = len(chunks) + 1
                    prefix_bytes = prefix.encode("utf-8")
                    prefix_hash = _sha256(prefix_bytes)
                    prefix_asset_id = self._upload(host, operation_key, prefix_bytes, "text/plain; charset=utf-8", f"stream-{seq}")
                    stream_prefix = {
                        "schema": "stream-prefix/v1",
                        "stream_id": _id("anthropic-stream", request_hash),
                        "job_id": normalized["job_id"],
                        "step_id": normalized["step_id"],
                        "output_role": normalized["output_role"],
                        "target": dict(normalized["target"]),
                        "attempt_id": normalized["attempt_id"],
                        "lease_epoch": normalized["lease_epoch"],
                        "prefix_seq": seq,
                        "prefix_asset_id": prefix_asset_id,
                        "prefix_hash": prefix_hash,
                        "byte_length": len(prefix_bytes),
                        "encoding": "utf-8",
                    }
                    ack = _host_call(
                        host,
                        "host.stream.commit/v1",
                        {"operation_key": operation_key, "stream_prefix_asset_id": prefix_asset_id},
                    )
                    if ack.get("accepted") is not True or ack.get("acked_prefix_seq") != seq or ack.get("acked_prefix_hash") != prefix_hash:
                        raise ProviderError("STREAM_ACK_INVALID", "Host ACK did not match the emitted prefix")
                    chunk = StreamChunk(
                        seq,
                        text,
                        prefix,
                        prefix_hash,
                        dict(usage) if usage else None,
                        prefix_asset_id,
                        stream_prefix,
                    )
                    chunks.append(chunk)
                    self._event(host, operation_key, "provider.chunk", prefix_asset_id, seq + 1)
                    self._checkpoint(host, normalized, request_hash, prefix_asset_id, seq)
                elif event_type == "error":
                    detail = event.get("error") if isinstance(event.get("error"), Mapping) else event
                    message = detail.get("message") if isinstance(detail, Mapping) else "Anthropic stream error"
                    raise ProviderError("UPSTREAM_ERROR", str(message or "Anthropic stream error"))
                elif event_type == "message_stop":
                    break
            if not prefix:
                raise ProviderError("EMPTY_MODEL_RESPONSE", "Anthropic returned no text")
            output_bytes = prefix.encode("utf-8")
            output_asset_id = self._upload(host, operation_key, output_bytes, "text/plain; charset=utf-8", "output")
            receipt_id = str(normalized.get("provenance_receipt_id") or _id("anthropic-receipt", request_hash))
            item = {
                "schema": "artifact-item/v1",
                "item_id": _id("anthropic-artifact", request_hash),
                "artifact_kind": "model-response",
                "payload_asset_id": output_asset_id,
                "payload_hash": _sha256(output_bytes),
                "mime": normalized.get("response_mime", "text/plain; charset=utf-8"),
                "source_refs": [],
                "status": "complete",
            }
            bundle: dict[str, Any] = {
                "schema": "result-bundle/v1",
                "contract_id": RESULT_CONTRACT,
                "bundle_id": _id("anthropic-bundle", request_hash),
                "bundle_type": "artifact",
                "producer": {
                    "plugin_id": PLUGIN_ID,
                    "release_id": RELEASE_ID,
                    "capability_id": ANTHROPIC_CAPABILITY_ID,
                    "job_id": normalized["job_id"],
                    "step_id": normalized["step_id"],
                    "attempt_id": normalized["attempt_id"],
                    "lease_epoch": normalized["lease_epoch"],
                },
                "input_snapshot_hash": normalized["run_snapshot_hash"],
                "items": [item],
                "warnings": [],
                "partial": False,
                "provenance_receipt_id": receipt_id,
                "skill_chain_result_refs": [],
            }
            bundle_bytes = _canonical_bytes(bundle)
            bundle_asset_id = self._upload(host, operation_key, bundle_bytes, "application/json", "result")
            bundle_hash = _hash_jcs("result-bundle/v1", bundle)
            receipt = self._receipt(normalized, request_hash, receipt_id, bundle["bundle_id"], bundle_hash)
            self._complete_host(host, normalized, operation_key, "succeeded", bundle_asset_id, None, len(chunks) + 1)
            return InvocationOutcome("succeeded", bundle, None, receipt, None, tuple(chunks), prefix)
        except _Cancelled:
            return self._cancelled_outcome(host, normalized, request_hash, tuple(chunks), prefix)
        except ProviderError as exc:
            return self._failure(host, normalized, request_hash, exc, tuple(chunks), prefix)
        except Exception as exc:
            return self._failure(
                host,
                normalized,
                request_hash,
                ProviderError("UPSTREAM_FAILURE", f"{type(exc).__name__}: {_safe_message(exc)}", retryable=True),
                tuple(chunks),
                prefix,
            )


def main() -> dict[str, Any]:
    """Control-plane entrypoint used by package discovery."""

    return capability_descriptor()
