from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "sdk", ROOT / "plugins" / "character-distillation", ROOT / "plugins" / "asset-derivation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from plotpilot_plugin_sdk.canonical import canonical_bytes  # noqa: E402
from character_distillation.runtime import (  # noqa: E402
    CAPABILITY_ATOM_EXTRACT,
    CAPABILITY_CARD_GENERATE,
    CAPABILITY_CONFLICT_APPLY,
    CAPABILITY_CONFLICT_REVIEW,
)
from asset_derivation.runtime import CAPABILITY_DATA_PACKAGE, CAPABILITY_TEMPLATE_DERIVE  # noqa: E402


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class MemoryHost:
    """Closed HostPort double used by every NAP-04 runtime test."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.uploads: dict[str, bytearray] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.stage_calls: list[dict[str, object]] = []
        self.completion_calls: list[dict[str, object]] = []
        self.checkpoint_calls: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self._event_seq = 0

    def seed_bytes(self, asset_id: str, raw: bytes) -> tuple[str, str]:
        self.assets[asset_id] = bytes(raw)
        return asset_id, sha256(raw)

    def seed_json(self, asset_id: str, value: object) -> tuple[str, str]:
        return self.seed_bytes(asset_id, canonical_bytes(value))

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        params = dict(params)
        self.calls.append((method, params))
        if method == "host.asset.read/v1":
            raw = self.assets[str(params["asset_id"])]
            offset = int(params["offset"])
            chunk = raw[offset : offset + int(params["length"])]
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": None if offset + len(chunk) >= len(raw) else offset + len(chunk),
                "content_hash": sha256(chunk),
            }
        if method == "host.asset.create/v1":
            upload_id = str(params["upload_id"])
            offset = int(params["offset"])
            chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
            buffer = self.uploads.setdefault(upload_id, bytearray())
            if offset != len(buffer):
                raise AssertionError("non-contiguous upload")
            buffer.extend(chunk)
            completed = bool(params["final"])
            asset_id = None
            if completed:
                raw = bytes(buffer)
                if len(raw) != int(params["total_size"]) or sha256(raw) != params["expected_hash"]:
                    raise AssertionError("upload digest mismatch")
                asset_id = "asset-" + sha256(raw)[:48]
                self.assets[asset_id] = raw
            return {
                "upload_id": upload_id,
                "accepted_bytes": len(buffer),
                "completed": completed,
                "asset_id": asset_id,
            }
        if method == "host.asset.upload.status/v1":
            raw = bytes(self.uploads[str(params["upload_id"])])
            return {"accepted_bytes": len(raw), "completed": True, "asset_id": "asset-" + sha256(raw)[:48]}
        if method == "host.job.event/v1":
            self._event_seq += 1
            self.events.append(params)
            return {"accepted": True, "job_event_seq": self._event_seq}
        if method == "host.checkpoint.commit/v1":
            self._event_seq += 1
            checkpoint = json.loads(self.assets[str(params["checkpoint_asset_id"])].decode("utf-8"))
            self.checkpoint_calls.append(params)
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self._event_seq,
            }
        if method == "host.candidate.stage/v1":
            self._event_seq += 1
            self.stage_calls.append(params)
            bundle = json.loads(self.assets[str(params["result_bundle_asset_id"])].decode("utf-8"))
            return {
                "accepted": True,
                "staged_items": [
                    {
                        "item_id": item["item_id"],
                        "candidate_id": "candidate-" + item["item_id"],
                        "stage_status": "created",
                        "publication_eligibility": "review_only",
                    }
                    for item in bundle["items"]
                ],
                "job_event_seq": self._event_seq,
            }
        if method == "host.job.complete/v1":
            self._event_seq += 1
            self.completion_calls.append(params)
            outcome = str(params["outcome"])
            return {
                "accepted": True,
                "attempt_state": outcome,
                "step_state": outcome,
                "job_state": outcome,
                "provenance_receipt_id": "receipt-test",
                "job_event_seq": self._event_seq,
                "core_event_high_water": self._event_seq,
            }
        raise AssertionError(f"unexpected Host method: {method}")


@pytest.fixture
def host() -> MemoryHost:
    return MemoryHost()


@pytest.fixture
def archetype() -> dict[str, object]:
    return json.loads(
        (ROOT / "data" / "character" / "character-archetype" / "v1" / "data" / "archetypes.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def source_text() -> str:
    return "阿宁抬头望向城门，随后握紧了手中的信。"


def character_request(
    host: MemoryHost,
    capability: str,
    *,
    operation: str = "run",
    source_text: str = "阿宁抬头望向城门，随后握紧了手中的信。",
    nodes: list[dict[str, int | str]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    text_bytes = source_text.encode("utf-8")
    text_hash = sha256(text_bytes)
    host.seed_bytes("canonical", text_bytes)
    archetype_path = ROOT / "data" / "character" / "character-archetype" / "v1" / "data" / "archetypes.json"
    archetype_bytes = archetype_path.read_bytes()
    host.seed_bytes("archetype", archetype_bytes)
    nodes = nodes or [{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": len(source_text)}]
    schemas = {
        CAPABILITY_ATOM_EXTRACT: "analysis.character.atom.extract-request/v1",
        CAPABILITY_CARD_GENERATE: "analysis.character.card.generate-request/v1",
        CAPABILITY_CONFLICT_REVIEW: "analysis.character.conflict.review-request/v1",
        CAPABILITY_CONFLICT_APPLY: "analysis.character.conflict.apply-request/v1",
    }
    request: dict[str, object] = {
        "schema": schemas[capability],
        "capability_id": capability,
        "operation_key": capability,
        "operation": operation,
        "job_id": "job-character",
        "step_id": "step-character",
        "attempt_id": "attempt-character",
        "worker_run_id": "worker-character",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-character"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "1" * 64,
        "workspace_id": "ws-character",
        "document_id": "doc-character",
        "source_revision_id": "rev-character",
        "canonical_asset_id": "canonical",
        "canonical_text_hash": text_hash,
        "nodes": deepcopy(nodes),
        "archetype_asset_id": "archetype",
        "archetype_asset_hash": sha256(archetype_bytes),
        "model_profile_revision_id": "model-character",
        "character_id": "character-ning",
    }
    groups: dict[str, dict[str, object]] = {
        CAPABILITY_ATOM_EXTRACT: {},
        CAPABILITY_CARD_GENERATE: {},
        CAPABILITY_CONFLICT_REVIEW: {},
        CAPABILITY_CONFLICT_APPLY: {},
    }
    if capability in (CAPABILITY_CARD_GENERATE,):
        groups[capability] = {"atom_asset_id": "atom", "atom_asset_hash": overrides.pop("atom_asset_hash", "0" * 64)}
    if capability in (CAPABILITY_CONFLICT_REVIEW, CAPABILITY_CONFLICT_APPLY):
        groups[capability] = {"conflict_asset_id": "conflict", "conflict_asset_hash": overrides.pop("conflict_asset_hash", "0" * 64)}
    if capability == CAPABILITY_CONFLICT_APPLY:
        groups[capability].update({"ruling_asset_id": "ruling", "ruling_asset_hash": overrides.pop("ruling_asset_hash", "0" * 64)})
    request.update(groups[capability])
    # The frozen conflict review/apply request schemas intentionally do not
    # carry a redundant character_id; the conflict Asset is the source of
    # that identity.  Keep this fixture byte-for-byte aligned with those
    # closed schemas so tests exercise the same request accepted by Core.
    if capability in (CAPABILITY_CONFLICT_REVIEW, CAPABILITY_CONFLICT_APPLY):
        request.pop("character_id", None)
    request.update(overrides)
    return request


def asset_request(
    host: MemoryHost,
    capability: str = CAPABILITY_TEMPLATE_DERIVE,
    *,
    operation: str = "run",
    source: object | None = None,
    source_format_id: str = "character-archetype/v1",
    target_format_id: str = "plot-structure-template/v1",
    **overrides: object,
) -> dict[str, object]:
    if source is None:
        source = json.loads((ROOT / "data" / "character" / "character-archetype" / "v1" / "data" / "archetypes.json").read_text(encoding="utf-8"))
    source_bytes = canonical_bytes(source)
    host.seed_bytes("source", source_bytes)
    request: dict[str, object] = {
        "schema": "asset.template.derive-request/v1" if capability == CAPABILITY_TEMPLATE_DERIVE else "asset.data-plugin.package-request/v1",
        "capability_id": capability,
        "operation_key": capability,
        "operation": operation,
        "job_id": "job-asset",
        "step_id": "step-asset",
        "attempt_id": "attempt-asset",
        "worker_run_id": "worker-asset",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-asset"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "2" * 64,
        "workspace_id": "ws-asset",
    }
    if capability == CAPABILITY_TEMPLATE_DERIVE:
        request.update({
            "source_asset_id": "source",
            "source_asset_hash": sha256(source_bytes),
            "source_format_id": source_format_id,
            "target_format_id": target_format_id,
            "target_schema_id": target_format_id,
            "model_profile_revision_id": "model-asset",
            "source_projection": {"source_format_id": source_format_id, "payload": deepcopy(source)},
        })
    else:
        raise AssertionError("package requests are built by package_request")
    request.update(overrides)
    return request


def package_request(host: MemoryHost, *, operation: str = "run", format_id: str = "character-archetype/v1", **overrides: object) -> dict[str, object]:
    root = ROOT / "data" / "character" / "character-archetype" / "v1"
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for relative in expected["package_files"]:
        raw = (root / relative).read_bytes()
        asset_id = "input-" + sha256(raw)[:40]
        host.seed_bytes(asset_id, raw)
        rows.append({"path": relative, "asset_id": asset_id, "sha256": sha256(raw), "mime": "application/json", "size": len(raw)})
    mapping = json.loads((root / "fixtures" / "interpreter-mappings.json").read_text(encoding="utf-8"))["mappings"]
    request: dict[str, object] = {
        "schema": "asset.data-plugin.package-request/v1",
        "capability_id": CAPABILITY_DATA_PACKAGE,
        "operation_key": CAPABILITY_DATA_PACKAGE,
        "operation": operation,
        "job_id": "job-package",
        "step_id": "step-package",
        "attempt_id": "attempt-package",
        "worker_run_id": "worker-package",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-package"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "3" * 64,
        "workspace_id": "ws-package",
        "format_id": format_id,
        "data_plugin_id": expected["plugin_id"],
        "version": expected["version"],
        "root_path": expected["root_path"],
        "files": rows,
        "package_hash": expected["package_hash"],
        "release_id": expected["release_id"],
        "interpreter_mappings": mapping,
    }
    request.update(overrides)
    return request
