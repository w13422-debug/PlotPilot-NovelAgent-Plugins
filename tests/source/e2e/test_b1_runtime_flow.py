from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "plugins" / "source-import"),
    str(ROOT / "plugins" / "source-cleaning-runtime"),
    str(ROOT / "plugins" / "source-structure"),
    str(ROOT / "sdk"),
]

from source_cleaning_runtime import (  # noqa: E402
    CAPABILITY_PREVIEW,
    make_cleaning_request,
    preview,
)
from source_import.worker import SourceImportPlugin  # noqa: E402
from source_structure.runtime import (  # noqa: E402
    CAPABILITY_SEARCH,
    StructurePlugin,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryCoreHost:
    """One accepted B0 HostPort shared by all three independent B1 plugins."""

    def __init__(self, assets: dict[str, bytes]) -> None:
        self.assets = dict(assets)
        self.uploads: dict[str, bytearray] = {}
        self.upload_assets: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.stage_calls: list[dict[str, object]] = []
        self.sequence = 0

    def _next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method == "host.asset.read/v1":
            data = self.assets[str(params["asset_id"])]
            offset = int(params["offset"])
            chunk = data[offset : offset + int(params["length"])]
            end = offset + len(chunk)
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": None if end == len(data) else end,
                "content_hash": _sha(chunk),
            }
        if method == "host.asset.create/v1":
            upload_id = str(params["upload_id"])
            offset = int(params["offset"])
            chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
            assert _sha(chunk) == params["chunk_hash"]
            buffer = self.uploads.setdefault(upload_id, bytearray())
            assert offset == len(buffer)
            buffer.extend(chunk)
            completed = bool(params["final"])
            asset_id = None
            if completed:
                payload = bytes(buffer)
                assert len(payload) == int(params["total_size"])
                assert _sha(payload) == params["expected_hash"]
                asset_id = f"asset-{_sha(payload)[:40]}"
                self.assets[asset_id] = payload
                self.upload_assets[upload_id] = asset_id
            return {
                "upload_id": upload_id,
                "accepted_bytes": len(buffer),
                "completed": completed,
                "asset_id": asset_id,
            }
        if method == "host.asset.upload.status/v1":
            upload_id = str(params["upload_id"])
            asset_id = self.upload_assets[upload_id]
            data = self.assets[asset_id]
            assert _sha(data) == params["expected_hash"]
            return {
                "accepted_bytes": len(data),
                "completed": True,
                "asset_id": asset_id,
            }
        if method == "host.job.event/v1":
            return {"accepted": True, "job_event_seq": self._next_sequence()}
        if method == "host.checkpoint.commit/v1":
            checkpoint = json.loads(
                self.assets[str(params["checkpoint_asset_id"])].decode("utf-8")
            )
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self._next_sequence(),
            }
        if method == "host.candidate.stage/v1":
            self.stage_calls.append(dict(params))
            bundle = json.loads(
                self.assets[str(params["result_bundle_asset_id"])].decode("utf-8")
            )
            rows = [
                {
                    "item_id": item["item_id"],
                    "candidate_id": f"candidate-{index}",
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                }
                for index, item in enumerate(bundle["items"])
            ]
            return {
                "accepted": True,
                "staged_items": rows,
                "job_event_seq": self._next_sequence(),
            }
        if method == "host.job.complete/v1":
            outcome = str(params["outcome"])
            sequence = self._next_sequence()
            return {
                "accepted": True,
                "attempt_state": outcome,
                "step_state": outcome,
                "job_state": outcome,
                "provenance_receipt_id": "receipt-e2e",
                "job_event_seq": sequence,
                "core_event_high_water": sequence,
            }
        raise AssertionError(f"unexpected Host RPC: {method}")


def test_imported_core_asset_is_consumed_independently_by_cleaning_and_structure() -> None:
    text = "第一章\n请牢记最新网址：https://example.invalid\n她在研究“广告”二字。\n"
    raw = text.encode("utf-8")
    host = MemoryCoreHost({"asset-raw": raw})
    snapshot = "1" * 64

    imported = SourceImportPlugin().inspect(
        {
            "schema": "source.import.inspect-request/v1",
            "capability_id": "source.import.inspect/v1",
            "worker_run_id": "run-import-e2e",
            "job_id": "job-import-e2e",
            "step_id": "step-import-e2e",
            "attempt_id": "attempt-import-e2e",
            "lease_epoch": 1,
            "run_snapshot_hash": snapshot,
            "source_asset_id": "asset-raw",
            "source_asset_hash": _sha(raw),
            "source_kind": "paste",
            "source_name": "paste.txt",
            "provenance_receipt_id": "receipt-e2e",
            "created_at": "2026-08-28T00:00:00Z",
            "page_size": 7,
        },
        host,
    )
    assert imported["status"] == "succeeded"
    canonical = next(
        item
        for item in imported["result"]["items"]
        if item["artifact_kind"] == "source-canonical-text"
    )
    canonical_asset_id = canonical["payload_asset_id"]
    canonical_hash = canonical["payload_hash"]
    canonical_text = host.assets[canonical_asset_id].decode("utf-8")
    assert canonical_text == text
    assert _sha(host.assets[canonical_asset_id]) == canonical_hash

    rules_root = ROOT / "data" / "source-cleaning" / "rules"
    rules = json.loads((rules_root / "data" / "rules.json").read_text(encoding="utf-8"))
    rules_expected = json.loads((rules_root / "expected.json").read_text(encoding="utf-8"))
    cleaning_request = make_cleaning_request(
        capability=CAPABILITY_PREVIEW,
        job_id="job-clean-e2e",
        step_id="step-clean-e2e",
        attempt_id="attempt-clean-e2e",
        worker_run_id="run-clean-e2e",
        lease_epoch=1,
        run_snapshot_hash=snapshot,
        workspace_id="workspace-e2e",
        document_id="document-e2e",
        revision_id="revision-e2e",
        base_content_hash=canonical_hash,
        source_asset_id=canonical_asset_id,
        source_asset_hash=canonical_hash,
        rules=rules,
        rules_release_id=rules_expected["release_id"],
        rules_package_hash=rules_expected["package_hash"],
        input_kind="paste",
        storage_source_format="paste",
        provenance_receipt_id="receipt-e2e",
        created_at="2026-08-28T00:00:00Z",
    )
    assert "source_text" not in cleaning_request
    cleaned = preview(cleaning_request, host)
    assert cleaned["contract_id"] == "artifact-bundle/v1"
    assert cleaned["items"]

    node = {
        "schema": "source-structure-node/v1",
        "node_id": "document-node",
        "node_kind": "chapter",
        "title": "第一章",
        "start_codepoint": 0,
        "end_codepoint": len(canonical_text),
        "body_start_codepoint": 0,
        "body_end_codepoint": len(canonical_text),
        "parent_node_id": None,
        "ordinal": 0,
        "removed": False,
    }
    searched = StructurePlugin().run(
        {
            "schema": "source.evidence.search-request/v1",
            "capability_id": CAPABILITY_SEARCH,
            "operation_key": CAPABILITY_SEARCH,
            "operation": "run",
            "job_id": "job-search-e2e",
            "step_id": "step-search-e2e",
            "attempt_id": "attempt-search-e2e",
            "worker_run_id": "run-search-e2e",
            "lease_epoch": 1,
            "checkpoint_ids": ["checkpoint-search-e2e"],
            "provenance_receipt_id": "receipt-e2e",
            "created_at": "2026-08-28T00:00:00Z",
            "total_units": 1,
            "run_snapshot_hash": snapshot,
            "workspace_id": "workspace-e2e",
            "document_id": "document-e2e",
            "revision_id": "revision-e2e",
            "canonical_asset_id": canonical_asset_id,
            "canonical_text_hash": canonical_hash,
            "nodes": [node],
            "query": "广告",
            "case_sensitive": True,
            "max_matches": 8,
        },
        host,
    )
    assert searched["contract_id"] == "artifact-bundle/v1"
    assert searched["items"]
    assert not host.stage_calls
    read_asset_ids = {
        str(params["asset_id"])
        for method, params in host.calls
        if method == "host.asset.read/v1"
    }
    assert {"asset-raw", canonical_asset_id}.issubset(read_asset_ids)
