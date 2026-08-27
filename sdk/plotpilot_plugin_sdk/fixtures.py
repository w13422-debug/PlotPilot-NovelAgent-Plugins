"""Shared deterministic fixtures for the six downstream project seams.

These are test-only helpers. They expose DTO/port behavior but never a Core
database handle, arbitrary filesystem root, or direct publication method.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_bytes, sha256_hex
from .errors import ContractError, ErrorCode
from .verifier import assert_valid


def payload_fingerprint(value: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_bytes(dict(value)))


def _tree_actions(node: Mapping[str, Any]) -> set[tuple[str, str]]:
    actions = {(str(event), str(event)) for event in node.get("event_ids", [])}
    for child in node.get("children", []):
        actions.update(_tree_actions(child))
    return actions


@dataclass
class PluginUIHostFixture:
    """In-memory Host-side UI wire fixture with stale/duplicate protection."""

    generation_id: str
    plugin_release_id: str
    workspace_id: str | None = None
    workspace_revision_id: str | None = None
    plan_revision_id: str | None = None
    installed_tree: dict[str, Any] | None = None
    intent_acks: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)

    def install_tree(self, tree: Mapping[str, Any]) -> None:
        assert_valid("plugin-ui-tree/v1", tree)
        if self.installed_tree is not None and tree["render_seq"] <= self.installed_tree["render_seq"]:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "UI render sequence is not monotonic")
        self.installed_tree = copy.deepcopy(dict(tree))

    def dispatch_intent(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        assert_valid("plugin-ui-intent/v1", intent)
        freshness = intent["freshness"]
        current = {
            "generation_id": self.generation_id,
            "plugin_release_id": self.plugin_release_id,
            "workspace_id": self.workspace_id,
            "workspace_revision_id": self.workspace_revision_id,
            "plan_revision_id": self.plan_revision_id,
        }
        if freshness != current or self.installed_tree is None or intent["render_seq"] != self.installed_tree["render_seq"]:
            return {
                "schema": "plugin-ui-ack/v1",
                "intent_id": intent["intent_id"],
                "accepted": False,
                "error_code": "stale_ui_freshness",
                "core_event_seq": None,
                "job_id": None,
            }
        actions = _tree_actions(self.installed_tree["root"])
        action_pair = (intent["action_id"], intent["event_type"])
        if action_pair not in actions and (intent["event_type"], intent["event_type"]) not in actions:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI intent is not declared by the installed tree")
        fingerprint = payload_fingerprint(dict(intent))
        previous = self.intent_acks.get(intent["intent_id"])
        if previous is not None:
            old_fingerprint, ack = previous
            if old_fingerprint != fingerprint:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "UI intent ID reused with a different payload")
            return copy.deepcopy(ack)
        ack = {
            "schema": "plugin-ui-ack/v1",
            "intent_id": intent["intent_id"],
            "accepted": True,
            "error_code": None,
            "core_event_seq": 1,
            "job_id": None,
        }
        self.intent_acks[intent["intent_id"]] = (fingerprint, ack)
        return copy.deepcopy(ack)


@dataclass
class HttpSSEFixture:
    """Small protocol fixture for P4 HTTP/SSE tests without a live server."""

    routes: dict[str, tuple[int, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_json(self, path: str, status: int, body: Any) -> None:
        self.routes[path] = (status, copy.deepcopy(body))

    def get(self, path: str) -> tuple[int, Any]:
        if path not in self.routes:
            raise ContractError(ErrorCode.ASSET_ERROR, f"fixture route not found: {path}")
        status, body = self.routes[path]
        return status, copy.deepcopy(body)

    def append_event(self, event: Mapping[str, Any]) -> None:
        self.events.append(copy.deepcopy(dict(event)))

    def sse_lines(self) -> list[str]:
        return [f"id: {event.get('event_id', index + 1)}\ndata: {canonical_bytes(event).decode('utf-8')}\n" for index, event in enumerate(self.events)]


__all__ = ["HttpSSEFixture", "PluginUIHostFixture", "payload_fingerprint"]
