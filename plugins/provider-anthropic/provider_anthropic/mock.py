"""Deterministic Anthropic transport and Core Host fixture.

The fixture deliberately exposes mutation knobs for contract negative tests;
it never opens a socket and never supplies caller-controlled credentials.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping

from plotpilot_plugin_sdk.canonical import canonical_bytes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ScriptedTransport:
    events: tuple[Mapping[str, Any], ...]
    error_at: int | None = None
    error: BaseException | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __init__(self, events: Iterable[Mapping[str, Any]], *, error_at: int | None = None, error: BaseException | None = None) -> None:
        self.events = tuple(deepcopy(dict(event)) for event in events)
        self.error_at = error_at
        self.error = error
        self.requests = []

    def stream(self, wire_request: Mapping[str, object]):
        self.requests.append(deepcopy(dict(wire_request)))
        for index, event in enumerate(self.events):
            if self.error_at == index:
                raise self.error or RuntimeError("scripted transport failure")
            yield deepcopy(dict(event))


@dataclass
class MemoryHost:
    """A strict-enough fake Host with explicit response mutation hooks."""

    assets: dict[str, bytes] = field(default_factory=dict)
    uploads: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    stream_prefixes: list[dict[str, object]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    job_events: list[dict[str, object]] = field(default_factory=list)
    completions: list[dict[str, object]] = field(default_factory=list)
    core_stream_id: str = "stream-core-anthropic"
    core_receipt_id: str = "receipt-core-anthropic"
    core_checkpoint_ids: tuple[str, ...] = ("checkpoint-core-anthropic-1", "checkpoint-core-anthropic-2", "checkpoint-core-anthropic-3")
    response_overrides: dict[str, dict[str, object]] = field(default_factory=dict)
    response_drops: dict[str, set[str]] = field(default_factory=dict)
    response_extras: dict[str, dict[str, object]] = field(default_factory=dict)
    reject_methods: set[str] = field(default_factory=set)

    def seed_json_asset(self, value: Mapping[str, Any], asset_id: str = "request-asset") -> str:
        self.assets[asset_id] = canonical_bytes(dict(value))
        return asset_id

    def _mutate(self, method: str, result: Mapping[str, object]) -> dict[str, object]:
        if method in self.reject_methods:
            raise AssertionError(f"deterministic Host rejection fixture: {method}")
        value = dict(result)
        value.update(self.response_overrides.get(method, {}))
        for field_name in self.response_drops.get(method, set()):
            value.pop(field_name, None)
        value.update(self.response_extras.get(method, {}))
        return value

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        value = dict(params)
        self.calls.append((method, value))
        if method == "host.asset.read/v1":
            data = self.assets[str(value["asset_id"])]
            offset = int(value.get("offset", 0))
            length = int(value.get("length", len(data)))
            chunk = data[offset:offset + length]
            return self._mutate(method, {"base64_chunk": base64.b64encode(chunk).decode("ascii"), "next_offset": offset + len(chunk), "content_hash": _sha256(data)})
        if method == "host.asset.create/v1":
            data = base64.b64decode(str(value["base64_chunk"]), validate=True)
            expected = str(value["expected_hash"])
            if _sha256(data) != expected or str(value["chunk_hash"]) != expected or value.get("offset") != 0 or value.get("final") is not True:
                raise AssertionError("invalid deterministic Asset upload")
            asset_id = f"asset-{expected[:20]}"
            self.assets[asset_id] = data
            self.uploads[str(value["upload_id"])] = asset_id
            return self._mutate(method, {"upload_id": value["upload_id"], "accepted_bytes": len(data), "completed": True, "asset_id": asset_id})
        if method == "host.asset.upload.status/v1":
            asset_id = self.uploads[str(value["upload_id"])]
            return self._mutate(method, {"accepted_bytes": len(self.assets[asset_id]), "completed": True, "asset_id": asset_id})
        if method == "host.stream.commit/v1":
            asset_id = str(value["stream_prefix_asset_id"])
            data = self.assets[asset_id]
            seq = len(self.stream_prefixes) + 1
            record = {"asset_id": asset_id, "prefix_seq": seq, "prefix_hash": _sha256(data), "byte_length": len(data)}
            self.stream_prefixes.append(record)
            return self._mutate(method, {"accepted": True, "stream_id": self.core_stream_id, "acked_prefix_seq": seq, "acked_bytes": len(data), "acked_prefix_hash": record["prefix_hash"], "job_event_seq": len(self.calls)})
        if method == "host.checkpoint.commit/v1":
            asset_id = str(value["checkpoint_asset_id"])
            checkpoint = json.loads(self.assets[asset_id].decode("utf-8"))
            self.checkpoints.append(checkpoint)
            seq = int(checkpoint["checkpoint_seq"])
            checkpoint_id = self.core_checkpoint_ids[seq - 1] if seq <= len(self.core_checkpoint_ids) else checkpoint["checkpoint_id"]
            return self._mutate(method, {"accepted": True, "checkpoint_id": checkpoint_id, "completed_units": checkpoint["completed_units"], "total_units": checkpoint["total_units"], "job_event_seq": len(self.calls)})
        if method == "host.job.event/v1":
            self.job_events.append(value)
            return self._mutate(method, {"accepted": True, "job_event_seq": len(self.calls)})
        if method == "host.job.complete/v1":
            self.completions.append(value)
            return self._mutate(method, {"accepted": True, "attempt_state": value["outcome"], "step_state": value["outcome"], "job_state": value["outcome"], "provenance_receipt_id": self.core_receipt_id, "job_event_seq": len(self.calls), "core_event_high_water": len(self.calls)})
        raise AssertionError(f"unexpected Host method: {method}")
