"""Typed ports and deterministic fakes for the P1/P2/P3 seams."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class CoreAuthorityPort(Protocol):
    def read_asset(self, asset_id: str) -> bytes: ...
    def create_asset(self, content: bytes, *, mime: str) -> str: ...
    def stage_candidate(self, operation_key: str, bundle_id: str) -> list[str]: ...
    def commit_checkpoint(self, operation_key: str, checkpoint_id: str) -> bool: ...


class PluginRuntimePort(Protocol):
    def resolve_release(self, plugin_id: str, release_requirement: str) -> str: ...
    def validate_settings(self, plugin_id: str, settings_revision_id: str) -> bool: ...
    def current_generation(self) -> str: ...


class ExecutionPort(Protocol):
    def start(self, capability_id: str, snapshot_asset_id: str) -> str: ...
    def poll(self, job_id: str, after_event_seq: int) -> tuple[bool, list[dict[str, object]]]: ...
    def cancel(self, operation_key: str, job_id: str) -> bool: ...


@dataclass
class FakeCoreAuthorityPort:
    assets: dict[str, bytes] = field(default_factory=dict)
    staged: dict[str, list[str]] = field(default_factory=dict)
    checkpoints: dict[str, str] = field(default_factory=dict)
    _asset_counter: int = 0

    def read_asset(self, asset_id: str) -> bytes:
        return self.assets[asset_id]

    def create_asset(self, content: bytes, *, mime: str) -> str:
        del mime
        self._asset_counter += 1
        asset_id = f"asset-fake-{self._asset_counter}"
        self.assets[asset_id] = bytes(content)
        return asset_id

    def stage_candidate(self, operation_key: str, bundle_id: str) -> list[str]:
        values = self.staged.setdefault(operation_key, [])
        if not values:
            values.append(bundle_id)
        elif values[0] != bundle_id:
            raise ValueError("operation key reused with a different candidate bundle")
        return list(self.staged[operation_key])

    def commit_checkpoint(self, operation_key: str, checkpoint_id: str) -> bool:
        self.checkpoints[operation_key] = checkpoint_id
        return True


@dataclass
class FakePluginRuntimePort:
    releases: dict[tuple[str, str], str] = field(default_factory=dict)
    settings: dict[tuple[str, str], bool] = field(default_factory=dict)
    generation_id: str = "generation-fake"

    def resolve_release(self, plugin_id: str, release_requirement: str) -> str:
        return self.releases[(plugin_id, release_requirement)]

    def validate_settings(self, plugin_id: str, settings_revision_id: str) -> bool:
        return self.settings.get((plugin_id, settings_revision_id), False)

    def current_generation(self) -> str:
        return self.generation_id


@dataclass
class FakeExecutionPort:
    jobs: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    _start_keys: dict[tuple[str, str], str] = field(default_factory=dict)

    def start(self, capability_id: str, snapshot_asset_id: str) -> str:
        key = (capability_id, snapshot_asset_id)
        if key in self._start_keys:
            return self._start_keys[key]
        job_id = f"job-fake-{len(self.jobs) + 1}"
        self._start_keys[key] = job_id
        self.jobs[job_id] = [{"capability_id": capability_id, "snapshot_asset_id": snapshot_asset_id, "state": "running"}]
        return job_id

    def poll(self, job_id: str, after_event_seq: int) -> tuple[bool, list[dict[str, object]]]:
        del after_event_seq
        events = self.jobs[job_id]
        return job_id in self.cancelled, list(events)

    def cancel(self, operation_key: str, job_id: str) -> bool:
        del operation_key
        self.cancelled.add(job_id)
        return True
