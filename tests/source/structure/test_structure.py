from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(
    0, str(ROOT / "plugins" / "source-structure")
)

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_provenance_receipt,
    verify_result_bundle,
)
from source_structure.contract import (  # noqa: E402
    EvidenceSpanError,
    apply_structure_operations,
    build_evidence_span,
    build_rebind_report,
    find_text_matches,
    rebind_evidence,
    sha256_text,
    validate_evidence_span,
)
from source_structure.runtime import (  # noqa: E402
    CAPABILITIES,
    CAPABILITY_REBIND_INSPECT,
    CAPABILITY_REBIND_PROPOSE,
    CAPABILITY_REVISE,
    CAPABILITY_SEARCH,
    DESCRIPTORS,
    StructurePlugin,
    TerminalContractError,
)


SCHEMA_BY_CAPABILITY = {
    CAPABILITY_REVISE: "source.structure.revise-request/v1",
    CAPABILITY_SEARCH: "source.evidence.search-request/v1",
    CAPABILITY_REBIND_INSPECT: (
        "source.evidence.rebind.inspect-request/v1"
    ),
    CAPABILITY_REBIND_PROPOSE: (
        "source.evidence.rebind.propose-request/v1"
    ),
}


def node(
    node_id: str,
    start: int,
    end: int,
    *,
    ordinal: int = 0,
    parent: str | None = None,
    title: str = "",
) -> dict:
    return {
        "schema": "source-structure-node/v1",
        "node_id": node_id,
        "node_kind": "chapter",
        "title": title or node_id,
        "start_codepoint": start,
        "end_codepoint": end,
        "body_start_codepoint": start,
        "body_end_codepoint": end,
        "parent_node_id": parent,
        "ordinal": ordinal,
        "removed": False,
    }


class MemoryHost:
    """Exact public RPC fixture with immutable Asset paging."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.uploads: dict[str, bytearray] = {}
        self.calls: list[tuple[str, dict]] = []
        self.read_calls: list[dict] = []
        self.event_calls: list[dict] = []
        self.checkpoint_calls: list[dict] = []
        self.stage_calls: list[dict] = []
        self.completion_calls: list[dict] = []
        self.lifecycle_sequences: list[int] = []
        self.receipt_id = "receipt-test"
        self.stage_mode = "valid"
        self.tamper_complete = False
        self.block_asset_id: str | None = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        self.block_release.set()
        self._block_consumed = False
        self._event_seq = 0

    def _next_seq(self) -> int:
        self._event_seq += 1
        self.lifecycle_sequences.append(self._event_seq)
        return self._event_seq

    @staticmethod
    def assert_keys(value: dict, expected: set[str]) -> None:
        if set(value) != expected:
            raise AssertionError(
                "RPC fields differ: "
                f"expected={sorted(expected)}, "
                f"actual={sorted(value)}"
            )

    def seed_text(self, asset_id: str, text: str) -> None:
        self.assets[asset_id] = text.encode("utf-8")

    def call(self, method: str, params: dict) -> dict:
        params = dict(params)
        self.calls.append((method, params))
        if method == "host.asset.read/v1":
            self.assert_keys(
                params, {"asset_id", "offset", "length"}
            )
            self.read_calls.append(params)
            if (
                params["asset_id"] == self.block_asset_id
                and not self._block_consumed
            ):
                self._block_consumed = True
                self.block_entered.set()
                if not self.block_release.wait(timeout=5):
                    raise AssertionError(
                        "test did not release blocked Asset read"
                    )
            data = self.assets[params["asset_id"]]
            offset = params["offset"]
            page = data[offset : offset + params["length"]]
            return {
                "base64_chunk": base64.b64encode(page).decode(
                    "ascii"
                ),
                "next_offset": (
                    None
                    if offset + len(page) >= len(data)
                    else offset + len(page)
                ),
                "content_hash": hashlib.sha256(page).hexdigest(),
            }
        if method == "host.asset.create/v1":
            self.assert_keys(
                params,
                {
                    "operation_key",
                    "upload_id",
                    "offset",
                    "mime",
                    "total_size",
                    "expected_hash",
                    "chunk_hash",
                    "base64_chunk",
                    "final",
                },
            )
            upload_id = params["upload_id"]
            chunk = base64.b64decode(
                params["base64_chunk"], validate=True
            )
            if (
                hashlib.sha256(chunk).hexdigest()
                != params["chunk_hash"]
            ):
                raise AssertionError("chunk hash mismatch")
            buffer = self.uploads.setdefault(
                upload_id, bytearray()
            )
            if params["offset"] != len(buffer):
                raise AssertionError("upload offset mismatch")
            buffer.extend(chunk)
            completed = bool(params["final"])
            asset_id = None
            if completed:
                data = bytes(buffer)
                if (
                    len(data) != params["total_size"]
                    or hashlib.sha256(data).hexdigest()
                    != params["expected_hash"]
                ):
                    raise AssertionError(
                        "final Asset identity mismatch"
                    )
                asset_id = (
                    "asset-"
                    + hashlib.sha256(data).hexdigest()[:40]
                )
                self.assets[asset_id] = data
            return {
                "upload_id": upload_id,
                "accepted_bytes": len(buffer),
                "completed": completed,
                "asset_id": asset_id,
            }
        if method == "host.asset.upload.status/v1":
            self.assert_keys(
                params, {"upload_id", "expected_hash"}
            )
            data = bytes(self.uploads[params["upload_id"]])
            asset_id = (
                "asset-" + hashlib.sha256(data).hexdigest()[:40]
            )
            return {
                "accepted_bytes": len(data),
                "completed": True,
                "asset_id": asset_id,
            }
        if method == "host.job.event/v1":
            self.assert_keys(
                params,
                {
                    "operation_key",
                    "event_type",
                    "payload_asset_id",
                    "local_seq",
                },
            )
            self.event_calls.append(params)
            return {
                "accepted": True,
                "job_event_seq": self._next_seq(),
            }
        if method == "host.checkpoint.commit/v1":
            self.assert_keys(
                params,
                {"operation_key", "checkpoint_asset_id"},
            )
            self.checkpoint_calls.append(params)
            checkpoint = json.loads(
                self.assets[
                    params["checkpoint_asset_id"]
                ].decode("utf-8")
            )
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint[
                    "completed_units"
                ],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self._next_seq(),
            }
        if method == "host.candidate.stage/v1":
            self.assert_keys(
                params,
                {
                    "operation_key",
                    "result_bundle_asset_id",
                    "input_snapshot_hash",
                },
            )
            self.stage_calls.append(params)
            bundle = json.loads(
                self.assets[
                    params["result_bundle_asset_id"]
                ].decode("utf-8")
            )
            rows = [
                {
                    "item_id": item["item_id"],
                    "candidate_id": (
                        "candidate-" + item["item_id"]
                    )[:128],
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                }
                for item in bundle["items"]
            ]
            if self.stage_mode in {"empty", "missing"}:
                rows = []
            elif self.stage_mode == "tampered":
                rows[0]["item_id"] = "tampered-item"
            elif self.stage_mode == "null-candidate":
                rows[0]["candidate_id"] = None
            elif self.stage_mode == "duplicate":
                rows = [rows[0], deepcopy(rows[0])]
            return {
                "accepted": True,
                "staged_items": rows,
                "job_event_seq": self._next_seq(),
            }
        if method == "host.job.complete/v1":
            self.assert_keys(
                params,
                {
                    "operation_key",
                    "worker_run_id",
                    "outcome",
                    "result_bundle_asset_id",
                    "candidate_stage_operation_key",
                    "terminal_detail_asset_id",
                    "local_seq",
                },
            )
            self.completion_calls.append(params)
            sequence = self._next_seq()
            state = params["outcome"]
            if self.tamper_complete:
                state = (
                    "failed"
                    if state != "failed"
                    else "succeeded"
                )
            return {
                "accepted": True,
                "attempt_state": state,
                "step_state": params["outcome"],
                "job_state": params["outcome"],
                "provenance_receipt_id": self.receipt_id,
                "job_event_seq": sequence,
                "core_event_high_water": sequence,
            }
        raise AssertionError(
            f"unexpected Host method: {method}"
        )


def common_context(
    capability: str,
    *,
    operation: str = "run",
    worker_run_id: str = "run-1",
) -> dict:
    return {
        "schema": SCHEMA_BY_CAPABILITY[capability],
        "capability_id": capability,
        "operation_key": capability,
        "operation": operation,
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "worker_run_id": worker_run_id,
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-1"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-27T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "1" * 64,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
    }


def revise_request(
    host: MemoryHost,
    *,
    operation: str = "run",
) -> dict:
    text = "章节正文"
    host.seed_text("asset-canonical", text)
    return {
        **common_context(
            CAPABILITY_REVISE, operation=operation
        ),
        "revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": sha256_text(text),
        "nodes": [node("document-node", 0, len(text))],
        "operations": [
            {
                "schema": "source-structure-operation/v1",
                "operation_id": "op-1",
                "type": "rename",
                "node_id": "document-node",
                "title": "新标题",
            }
        ],
    }


def search_request(
    host: MemoryHost,
    *,
    text: str = "甲😀目标乙",
    query: str = "目标",
) -> dict:
    host.seed_text("asset-canonical", text)
    return {
        **common_context(CAPABILITY_SEARCH),
        "revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": sha256_text(text),
        "nodes": [node("document-node", 0, len(text))],
        "query": query,
        "case_sensitive": True,
        "max_matches": 64,
    }


def rebind_request(
    host: MemoryHost,
    capability: str,
    *,
    operation: str = "run",
    worker_run_id: str = "run-1",
    span_revision_id: str = "rev-1",
) -> dict:
    source = "甲目标乙"
    target = "前甲目标乙后"
    host.seed_text("asset-source", source)
    host.seed_text("asset-target", target)
    source_nodes = [node("n-1", 0, len(source))]
    target_nodes = [node("n-2", 0, len(target))]
    span = build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id=span_revision_id,
        node_id="n-1",
        canonical_text=source,
        start_codepoint=1,
        end_codepoint=3,
        node_range={
            "start_codepoint": 0,
            "end_codepoint": len(source),
        },
    )
    return {
        **common_context(
            capability,
            operation=operation,
            worker_run_id=worker_run_id,
        ),
        "source_revision_id": "rev-1",
        "source_canonical_asset_id": "asset-source",
        "source_canonical_text_hash": sha256_text(source),
        "target_revision_id": "rev-2",
        "target_canonical_asset_id": "asset-target",
        "target_canonical_text_hash": sha256_text(target),
        "source_nodes": source_nodes,
        "target_nodes": target_nodes,
        "evidence_items": [
            {
                "schema": "source-evidence-item/v1",
                "evidence_id": "ev-1",
                "span": span,
                "parent_candidate_id": None,
            }
        ],
        "selected_evidence_ids": ["ev-1"],
        "known_parent_candidate_ids": [],
    }


class StructureContractTests(unittest.TestCase):
    def test_unicode_scalar_span_is_exact_and_hash_bound(
        self,
    ) -> None:
        text = "A😀B"
        span = build_evidence_span(
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            node_id="node-1",
            canonical_text=text,
            start_codepoint=1,
            end_codepoint=2,
            node_range={
                "start_codepoint": 0,
                "end_codepoint": 3,
            },
        )
        self.assertEqual(span["quote"], "😀")
        self.assertEqual(
            (
                span["start_codepoint"],
                span["end_codepoint"],
            ),
            (1, 2),
        )
        self.assertEqual(
            span["quote_hash"],
            hashlib.sha256("😀".encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            span["canonical_text_hash"], sha256_text(text)
        )
        bad = dict(span)
        bad["quote"] = "B"
        with self.assertRaises(EvidenceSpanError):
            validate_evidence_span(bad, text)

    def test_casefold_expansion_deduplicates_scalar_span(
        self,
    ) -> None:
        matches = find_text_matches(
            canonical_text="ß",
            query="s",
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            nodes=[node("n-1", 0, 1)],
            case_sensitive=False,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            (
                matches[0]["span"]["start_codepoint"],
                matches[0]["span"]["end_codepoint"],
            ),
            (0, 1),
        )

    def test_structure_revision_is_deterministic_and_pure(
        self,
    ) -> None:
        text = "章节正文"
        original = [node("n-1", 0, len(text))]
        operations = [
            {
                "type": "rename",
                "node_id": "n-1",
                "title": "新标题",
            }
        ]
        first = apply_structure_operations(
            original, operations, text_length=len(text)
        )
        second = apply_structure_operations(
            original, operations, text_length=len(text)
        )
        self.assertEqual(first, second)
        self.assertEqual(original[0]["title"], "n-1")
        self.assertEqual(first[0]["title"], "新标题")

    def test_rebind_has_four_classes_and_exact_lineage(
        self,
    ) -> None:
        source = "甲目标乙"
        source_nodes = [node("n-1", 0, len(source))]
        span = build_evidence_span(
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            node_id="n-1",
            canonical_text=source,
            start_codepoint=1,
            end_codepoint=3,
            node_range={
                "start_codepoint": 0,
                "end_codepoint": len(source),
            },
        )
        item = {
            "schema": "source-evidence-item/v1",
            "evidence_id": "ev-1",
            "span": span,
            "parent_candidate_id": None,
        }
        common = {
            "expected_workspace_id": "ws-1",
            "expected_document_id": "doc-1",
            "expected_source_revision_id": "rev-1",
        }
        unchanged = rebind_evidence(
            item,
            source,
            source,
            source_nodes,
            source_nodes,
            target_revision_id="rev-1",
            **common,
        )
        rebound = rebind_evidence(
            item,
            source,
            "前甲目标乙后",
            source_nodes,
            [node("n-2", 0, len("前甲目标乙后"))],
            target_revision_id="rev-2",
            **common,
        )
        rerun = rebind_evidence(
            item,
            source,
            "目标中目标",
            source_nodes,
            [node("n-3", 0, 5)],
            target_revision_id="rev-3",
            **common,
        )
        orphaned = rebind_evidence(
            item,
            source,
            "完全不同",
            source_nodes,
            [node("n-4", 0, 4)],
            target_revision_id="rev-4",
            **common,
        )
        self.assertEqual(
            [
                unchanged["classification"],
                rebound["classification"],
                rerun["classification"],
                orphaned["classification"],
            ],
            [
                "unchanged",
                "rebound",
                "needs_rerun",
                "orphaned",
            ],
        )
        self.assertTrue(rebound["candidate_eligible"])
        report = build_rebind_report(
            workspace_id="ws-1",
            document_id="doc-1",
            source_revision_id="rev-1",
            source_canonical_text_hash=sha256_text(source),
            target_revision_id="rev-2",
            target_canonical_text_hash=sha256_text(
                "前甲目标乙后"
            ),
            items=[rebound],
        )
        self.assertEqual(report["counts"]["rebound"], 1)
        with self.assertRaises(EvidenceSpanError):
            rebind_evidence(
                item,
                source,
                source,
                source_nodes,
                source_nodes,
                target_revision_id="rev-1",
                expected_workspace_id="ws-1",
                expected_document_id="doc-1",
                expected_source_revision_id="rev-X",
            )


class StructureRuntimeTests(unittest.TestCase):
    def test_raw_text_and_path_authority_fields_fail_closed(
        self,
    ) -> None:
        for field, value in (
            ("canonical_text", "伪造正文"),
            ("canonical_path", "C:/forbidden.txt"),
            ("file_path", "C:/forbidden.txt"),
        ):
            with self.subTest(field=field):
                plugin = StructurePlugin()
                host = MemoryHost()
                request = search_request(host)
                request[field] = value
                result = plugin.run(request, host)
                self.assertEqual(
                    result["contract_id"],
                    "diagnostic-bundle/v1",
                )
                self.assertEqual(host.read_calls, [])
                self.assertEqual(host.stage_calls, [])
                self.assertEqual(
                    host.completion_calls[-1]["outcome"],
                    "failed",
                )
        for field in (
            "source_canonical_text",
            "target_canonical_text",
            "source_canonical_path",
        ):
            with self.subTest(field=field):
                plugin = StructurePlugin()
                host = MemoryHost()
                request = rebind_request(
                    host, CAPABILITY_REBIND_PROPOSE
                )
                request[field] = "forbidden"
                result = plugin.run(request, host)
                self.assertEqual(
                    result["contract_id"],
                    "diagnostic-bundle/v1",
                )
                self.assertEqual(host.read_calls, [])
                self.assertEqual(host.stage_calls, [])

    def test_rebind_lineage_mismatch_rejected_before_classification(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        request = rebind_request(
            host,
            CAPABILITY_REBIND_PROPOSE,
            span_revision_id="rev-X",
        )
        result = plugin.run(request, host)
        self.assertEqual(
            result["contract_id"], "diagnostic-bundle/v1"
        )
        self.assertEqual(
            result["items"][0]["code"],
            "EVIDENCE_LINEAGE_INVALID",
        )
        self.assertEqual(host.stage_calls, [])
        self.assertEqual(
            host.completion_calls[-1]["outcome"], "failed"
        )

    def test_inspect_and_propose_lifecycle_and_receipts(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        inspect_request = rebind_request(
            host, CAPABILITY_REBIND_INSPECT
        )
        inspect_result = plugin.run(inspect_request, host)
        self.assertEqual(
            inspect_result["contract_id"],
            "diagnostic-bundle/v1",
        )
        verify_result_bundle(
            inspect_result,
            snapshot_hash_value="1" * 64,
        )
        self.assertEqual(host.stage_calls, [])
        self.assertEqual(len(host.checkpoint_calls), 1)
        self.assertIsNotNone(plugin.last_checkpoint)
        self.assertEqual(plugin.last_receipt["staged_items"], [])
        verify_provenance_receipt(plugin.last_receipt)
        self.assertEqual(
            host.completion_calls[-1]["outcome"],
            "succeeded",
        )

        propose_request = rebind_request(
            host, CAPABILITY_REBIND_PROPOSE
        )
        propose_result = plugin.run(propose_request, host)
        self.assertEqual(
            propose_result["contract_id"],
            "candidate-batch/v1",
        )
        self.assertEqual(len(host.stage_calls), 1)
        self.assertEqual(
            plugin.last_receipt["staged_items"],
            [
                item["item_id"]
                for item in propose_result["items"]
            ],
        )
        verify_provenance_receipt(plugin.last_receipt)
        self.assertEqual(
            plugin.last_stage_response["staged_items"][0][
                "item_id"
            ],
            propose_result["items"][0]["item_id"],
        )
        completion = host.completion_calls[-1]
        self.assertEqual(
            completion["candidate_stage_operation_key"],
            host.stage_calls[-1]["operation_key"],
        )
        self.assertEqual(
            completion["result_bundle_asset_id"],
            host.stage_calls[-1]["result_bundle_asset_id"],
        )
        self.assertEqual(
            host.lifecycle_sequences,
            sorted(set(host.lifecycle_sequences)),
        )

    def test_validate_never_stages_or_completes(
        self,
    ) -> None:
        plugin = StructurePlugin()
        revise_host = MemoryHost()
        revise = plugin.run(
            revise_request(revise_host, operation="validate"),
            revise_host,
        )
        self.assertEqual(
            revise["contract_id"], "candidate-batch/v1"
        )
        self.assertEqual(revise_host.stage_calls, [])
        self.assertEqual(revise_host.completion_calls, [])
        self.assertEqual(revise_host.event_calls, [])

        propose_host = MemoryHost()
        propose = plugin.run(
            rebind_request(
                propose_host,
                CAPABILITY_REBIND_PROPOSE,
                operation="validate",
            ),
            propose_host,
        )
        self.assertEqual(
            propose["contract_id"], "candidate-batch/v1"
        )
        self.assertEqual(propose_host.stage_calls, [])
        self.assertEqual(propose_host.completion_calls, [])
        self.assertEqual(propose_host.event_calls, [])

    def test_search_uses_asset_rpc_and_never_stages(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        result = plugin.run(search_request(host), host)
        self.assertEqual(
            result["contract_id"], "artifact-bundle/v1"
        )
        self.assertEqual(host.read_calls[0]["asset_id"], "asset-canonical")
        self.assertEqual(host.stage_calls, [])
        self.assertEqual(
            plugin.last_receipt["staged_items"], []
        )
        verify_provenance_receipt(plugin.last_receipt)
        self.assertEqual(
            [call["local_seq"] for call in host.event_calls],
            [1, 2],
        )
        self.assertEqual(
            host.completion_calls[-1]["local_seq"], 3
        )

    def test_stage_rows_must_be_exact_nonempty_and_one_to_one(
        self,
    ) -> None:
        for mode in (
            "empty",
            "missing",
            "tampered",
            "null-candidate",
            "duplicate",
        ):
            with self.subTest(mode=mode):
                plugin = StructurePlugin()
                host = MemoryHost()
                host.stage_mode = mode
                result = plugin.run(revise_request(host), host)
                self.assertEqual(
                    result["contract_id"],
                    "diagnostic-bundle/v1",
                )
                self.assertEqual(
                    result["items"][0]["code"],
                    "CANDIDATE_STAGE_ERROR",
                )
                self.assertEqual(len(host.stage_calls), 1)
                self.assertEqual(
                    host.completion_calls[-1]["outcome"],
                    "failed",
                )
                self.assertEqual(
                    plugin.last_receipt["staged_items"], []
                )

    def test_inspect_resume_reads_checkpoint_state_result_only(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        request = rebind_request(
            host, CAPABILITY_REBIND_INSPECT
        )
        original = plugin.run(request, host)
        checkpoint = deepcopy(plugin.last_checkpoint)
        self.assertIsNotNone(checkpoint)
        del host.assets["asset-source"]
        del host.assets["asset-target"]
        host.read_calls.clear()
        resume = {
            **request,
            "operation": "resume",
            "resume_checkpoint_asset_id": checkpoint[
                "checkpoint_asset_id"
            ],
            "resume_checkpoint_asset_hash": checkpoint[
                "checkpoint_asset_hash"
            ],
            "resume_state_asset_id": checkpoint[
                "state_asset_id"
            ],
            "resume_state_asset_hash": checkpoint[
                "state_asset_hash"
            ],
        }
        resumed = plugin.run(resume, host)
        self.assertEqual(resumed, original)
        read_ids = [
            call["asset_id"] for call in host.read_calls
        ]
        self.assertEqual(
            read_ids,
            [
                checkpoint["checkpoint_asset_id"],
                checkpoint["state_asset_id"],
                original["items"][0]["details_asset_id"]
                if False
                else json.loads(
                    host.assets[
                        checkpoint["state_asset_id"]
                    ].decode("utf-8")
                )["result_bundle_asset_id"],
            ],
        )
        self.assertNotIn("asset-source", read_ids)
        self.assertNotIn("asset-target", read_ids)
        self.assertEqual(host.stage_calls, [])

    def test_inspect_cancel_checkpoint_then_resume_without_recompute(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        request = rebind_request(
            host,
            CAPABILITY_REBIND_INSPECT,
            worker_run_id="run-cancel",
        )
        host.block_asset_id = "asset-source"
        host.block_release.clear()
        returned: list[object] = []
        failed: list[BaseException] = []

        def invoke() -> None:
            try:
                returned.append(plugin.run(request, host))
            except BaseException as exc:  # pragma: no cover
                failed.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        self.assertTrue(host.block_entered.wait(timeout=5))
        cancelled = plugin.run(
            {**request, "operation": "cancel"}, host
        )
        self.assertEqual(
            cancelled,
            {
                "accepted": True,
                "worker_run_id": "run-cancel",
            },
        )
        host.block_release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failed, [])
        self.assertEqual(returned, [None])
        self.assertEqual(
            host.completion_calls[-1]["outcome"],
            "cancelled",
        )
        checkpoint = deepcopy(plugin.last_checkpoint)
        state = json.loads(
            host.assets[
                checkpoint["state_asset_id"]
            ].decode("utf-8")
        )
        self.assertEqual(state["status"], "cancelled")
        del host.assets["asset-source"]
        del host.assets["asset-target"]
        host.read_calls.clear()
        resumed = plugin.run(
            {
                **request,
                "operation": "resume",
                "resume_checkpoint_asset_id": checkpoint[
                    "checkpoint_asset_id"
                ],
                "resume_checkpoint_asset_hash": checkpoint[
                    "checkpoint_asset_hash"
                ],
                "resume_state_asset_id": checkpoint[
                    "state_asset_id"
                ],
                "resume_state_asset_hash": checkpoint[
                    "state_asset_hash"
                ],
            },
            host,
        )
        self.assertEqual(
            resumed["contract_id"],
            "diagnostic-bundle/v1",
        )
        self.assertNotIn(
            "asset-source",
            [call["asset_id"] for call in host.read_calls],
        )
        self.assertEqual(
            host.completion_calls[-1]["outcome"],
            "succeeded",
        )

    def test_resume_binding_tamper_fails_closed(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        request = rebind_request(
            host, CAPABILITY_REBIND_INSPECT
        )
        plugin.run(request, host)
        checkpoint = deepcopy(plugin.last_checkpoint)
        resume = {
            **request,
            "operation": "resume",
            "source_revision_id": "rev-tampered",
            "resume_checkpoint_asset_id": checkpoint[
                "checkpoint_asset_id"
            ],
            "resume_checkpoint_asset_hash": checkpoint[
                "checkpoint_asset_hash"
            ],
            "resume_state_asset_id": checkpoint[
                "state_asset_id"
            ],
            "resume_state_asset_hash": checkpoint[
                "state_asset_hash"
            ],
        }
        result = plugin.run(resume, host)
        self.assertEqual(
            result["contract_id"], "diagnostic-bundle/v1"
        )
        self.assertEqual(
            result["items"][0]["code"], "CHECKPOINT_INVALID"
        )
        self.assertEqual(host.stage_calls, [])

    def test_terminal_host_failure_is_not_swallowed(
        self,
    ) -> None:
        plugin = StructurePlugin()
        host = MemoryHost()
        host.tamper_complete = True
        with self.assertRaises(TerminalContractError):
            plugin.run(search_request(host), host)

    def test_descriptors_cover_all_real_capabilities(
        self,
    ) -> None:
        self.assertEqual(set(DESCRIPTORS), set(CAPABILITIES))
        for capability in CAPABILITIES:
            descriptor = DESCRIPTORS[capability]
            self.assertEqual(
                descriptor["capability_id"], capability
            )
            self.assertEqual(
                descriptor["provider"]["plugin_id"],
                "com.plotpilot.novelagent.source-structure",
            )
            self.assertEqual(
                descriptor["accepted_data_formats"], []
            )


if __name__ == "__main__":
    unittest.main()
