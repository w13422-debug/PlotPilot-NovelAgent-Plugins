"""Deterministic, network-free provider used by all automatic tests."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .errors import ContractError, ErrorCode


@dataclass(frozen=True)
class FakeStreamChunk:
    seq: int
    text: str
    prefix_hash: str


@dataclass(frozen=True)
class FakeProviderReceipt:
    invocation_id: str
    request_hash: str
    response_hash: str | None
    state: str
    model: str
    calls: int


@dataclass
class FakeProviderRun:
    run_id: str
    invocation_id: str
    request_hash: str
    chunks: tuple[str, ...]
    state: str = "running"
    emitted_seq: int = 0
    acked_seq: int = 0
    calls: int = 1


@dataclass
class FakeProvider:
    """A provider with deterministic stream, error, pause and cancel modes."""

    model: str = "fake-deterministic-v1"
    runs: dict[str, FakeProviderRun] = field(default_factory=dict)
    _counter: int = 0

    def start(self, invocation_id: str, request: str, *, mode: str = "stream", chunks: Iterable[str] | None = None) -> FakeProviderRun:
        self._counter += 1
        request_hash = hashlib.sha256(request.encode("utf-8")).hexdigest()
        if mode == "error":
            run = FakeProviderRun(f"fake-run-{self._counter}", invocation_id, request_hash, (), state="failed")
        else:
            selected = tuple(chunks or ("fake", " provider", " output"))
            run = FakeProviderRun(f"fake-run-{self._counter}", invocation_id, request_hash, selected, state="running")
        self.runs[run.run_id] = run
        return run

    def stream(self, run_id: str) -> Iterator[FakeStreamChunk]:
        run = self.runs[run_id]
        prefix = ""
        for seq, chunk in enumerate(run.chunks, start=1):
            if run.state in {"cancelled", "failed", "paused"}:
                break
            prefix += chunk
            run.emitted_seq = seq
            # The convenience stream models a consumer that ACKs each durable
            # prefix immediately. ``raw_stream`` below is used for ACK-loss
            # and crash tests where emission and acknowledgement are split.
            run.acked_seq = seq
            yield FakeStreamChunk(seq, prefix, hashlib.sha256(prefix.encode("utf-8")).hexdigest())
        if run.state == "running":
            run.state = "succeeded"

    def raw_stream(self, run_id: str) -> Iterator[FakeStreamChunk]:
        """Emit deterministic prefixes without pretending the Host ACKed them."""
        run = self.runs[run_id]
        prefix = ""
        for seq, chunk in enumerate(run.chunks, start=1):
            if run.state in {"cancelled", "failed", "paused"}:
                break
            prefix += chunk
            run.emitted_seq = seq
            yield FakeStreamChunk(seq, prefix, hashlib.sha256(prefix.encode("utf-8")).hexdigest())

    def acknowledge(self, run_id: str, seq: int) -> None:
        run = self.runs[run_id]
        if seq < run.acked_seq or seq > run.emitted_seq:
            raise ValueError("ACK sequence is outside the emitted prefix")
        run.acked_seq = seq

    def pause(self, run_id: str) -> None:
        run = self.runs[run_id]
        if run.state == "running":
            run.state = "paused"

    def resume(self, run_id: str) -> FakeProviderRun:
        run = self.runs[run_id]
        if run.state == "paused":
            run.state = "running"
            run.calls += 1
        elif run.state in {"succeeded", "failed", "cancelled"}:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal provider run cannot be resumed")
        return run

    def cancel(self, run_id: str) -> None:
        run = self.runs[run_id]
        if run.state in {"succeeded", "failed", "cancelled"}:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal provider run cannot be cancelled")
        run.state = "cancelled"

    def receipt(self, run_id: str) -> FakeProviderReceipt:
        run = self.runs[run_id]
        response_hash = None
        if run.acked_seq:
            response = "".join(run.chunks)
            response_hash = hashlib.sha256(response.encode("utf-8")).hexdigest()
        return FakeProviderReceipt(run.invocation_id, run.request_hash, response_hash, run.state, self.model, run.calls)
