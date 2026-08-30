from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
PLUGIN_ROOT = ROOT / "plugins" / "donor-analysis"
for path in (SDK_ROOT, PLUGIN_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from plotpilot_plugin_sdk.canonical import canonical_bytes  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_checkpoint,
    verify_provenance_receipt,
    verify_result_bundle,
)
from donor_analysis.contract import (  # noqa: E402
    build_atom_provenance,
    build_evidence_span,
    hash_json,
    sha256_text,
)
from donor_analysis.runtime import (  # noqa: E402
    CAPABILITY_ATOM_EXTRACT,
    CAPABILITY_ATOM_MANUAL,
    CAPABILITY_CLAIM_GENERATE,
    CAPABILITY_REREVIEW,
    DonorAnalysisPlugin,
    TerminalContractError,
)


TEXT = "甲😀e\u0301乙。动作骤停。"
TEXT_BYTES = TEXT.encode("utf-8")
TEXT_HASH = hashlib.sha256(TEXT_BYTES).hexdigest()
NODES = [{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": len(TEXT)}]
TAXONOMY_PATH = ROOT / "data" / "book-analysis-taxonomy" / "v1" / "data" / "taxonomy.json"
TAXONOMY_BYTES = TAXONOMY_PATH.read_bytes()
TAXONOMY_HASH = hashlib.sha256(TAXONOMY_BYTES).hexdigest()


def evidence() -> dict:
    return build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id="node-1",
        start_codepoint=1,
        end_codepoint=4,
        canonical_text=TEXT,
    )


def atom_payload(*, manual: bool = False) -> dict:
    return {
        "schema": "book-atom/v1",
        "atom_kind": "action",
        "title": "动作骤停",
        "observation": "动作突然停止",
        "interpretation": "形成节奏顿挫",
        "applicability": "章节转折",
        "limitations": "需要上下文",
        "source_category": "user_annotation" if manual else "original_observation",
        "observation_confidence": "confirmed",
        "interpretation_confidence": "inferred",
        "evidence_spans": [evidence()],
        "tags": ["动作"],
        "analysis_method": {
            "name": "book-atom-manual" if manual else "book-atom-extract",
            "version": "1",
        },
        "provenance": None,
        "prompt_eligible": False,
        "promotion_authorized": False,
        "authority": "candidate_only",
    }


def trusted_atom_provenance(*, manual: bool = False) -> dict:
    capability = CAPABILITY_ATOM_MANUAL if manual else CAPABILITY_ATOM_EXTRACT
    return build_atom_provenance(
        mode="manual" if manual else "model",
        analysis_method={"name": "book-atom-manual" if manual else "book-atom-extract", "version": "1"},
        plugin_id="com.plotpilot.novelagent.donor-analysis",
        package_hash="a" * 64, release_id="b" * 64, capability_id=capability,
        job_id="job-source", step_id="step-source", attempt_id="attempt-source",
        worker_run_id="worker-source", lease_epoch=1,
        provenance_receipt_id="receipt-source", run_snapshot_hash="c" * 64,
        workspace_id="ws-1", document_id="doc-1", source_revision_id="rev-1",
        canonical_text_hash=TEXT_HASH, created_at="2026-08-30T00:00:00Z",
        model_profile_revision_id=None if manual else "model-profile-source",
        model_receipt_id=None if manual else "model-receipt-source",
        actor_id="user-source" if manual else None,
        annotation_asset_id="asset-annotation-source" if manual else None,
        annotation_asset_hash="d" * 64 if manual else None,
    )


def claim_input() -> dict:
    return {
        "schema": "claim-input/v1",
        "source_revision_id": "rev-1",
        "ordered_atoms": [
            {
                "ordinal": 0,
                "atom_id": "atom-accepted-1",
                "payload_hash": "0" * 64,  # rebound by seed_claim_assets
                "acceptance_ordinal": 11,
                "evidence_spans": [evidence()],
            }
        ],
    }


def claim_payload(frozen: dict) -> dict:
    return {
        "schema": "book-claim/v1",
        "category": "rhythm",
        "title": "顿挫节奏",
        "claim": "骤停动作形成顿挫",
        "scope": "chapter",
        "applicability": "转折处",
        "counterexamples": [],
        "conflicts": [],
        "interpretation_confidence": "inferred",
        "ordered_atom_ids": [item["atom_id"] for item in frozen["ordered_atoms"]],
        "evidence_spans": [
            deepcopy(span)
            for item in frozen["ordered_atoms"]
            for span in item["evidence_spans"]
        ],
        "analysis_method": {"name": "book-claim-generate", "version": "1"},
        "prompt_eligible": False,
        "promotion_authorized": False,
        "authority": "candidate_only",
    }


class Host:
    """Closed public Host RPC fake with durable operation-key replay."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {
            "asset-canonical": TEXT_BYTES,
            "asset-taxonomy": TAXONOMY_BYTES,
            "asset-annotation": "人工注释".encode("utf-8"),
        }
        self.uploads: dict[str, bytearray] = {}
        self.upload_results: dict[tuple[str, int], tuple[str, dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.event_calls: list[dict] = []
        self.checkpoint_calls: list[dict] = []
        self.stage_calls: list[dict] = []
        self.completion_calls: list[dict] = []
        self.logical_stage_results: dict[str, dict] = {}
        self.model_outputs: dict[str, dict] = {
            "analysis.book.atom.extract-model-response/v1": {
                "schema": "analysis.book.atom.extract-model-response/v1",
                "items": [atom_payload()],
            }
        }
        self.model_failures_remaining = 0
        self.completion_ack_mode: str | None = None
        self.interrupt_stage_after_accept = False
        self.block_checkpoint = False
        self.checkpoint_entered = threading.Event()
        self.checkpoint_release = threading.Event()
        self.checkpoint_release.set()
        self._event_seq = 0

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    @staticmethod
    def assert_keys(value: dict, expected: set[str]) -> None:
        assert set(value) == expected, (sorted(value), sorted(expected))

    def seed_json(self, asset_id: str, value) -> tuple[str, str]:
        data = canonical_bytes(value)
        self.assets[asset_id] = data
        return asset_id, hashlib.sha256(data).hexdigest()

    def call(self, method: str, params: dict) -> dict:
        params = dict(params)
        self.calls.append((method, params))
        if method == "host.asset.read/v1":
            self.assert_keys(params, {"asset_id", "offset", "length"})
            data = self.assets[params["asset_id"]]
            offset = params["offset"]
            page = data[offset : offset + params["length"]]
            return {
                "base64_chunk": base64.b64encode(page).decode("ascii"),
                "next_offset": None if offset + len(page) >= len(data) else offset + len(page),
                "content_hash": hashlib.sha256(page).hexdigest(),
            }
        if method == "host.asset.create/v1":
            self.assert_keys(
                params,
                {
                    "operation_key", "upload_id", "offset", "mime", "total_size",
                    "expected_hash", "chunk_hash", "base64_chunk", "final",
                },
            )
            replay_key = (params["upload_id"], params["offset"])
            payload_hash = hashlib.sha256(canonical_bytes(params)).hexdigest()
            previous = self.upload_results.get(replay_key)
            if previous is not None:
                assert previous[0] == payload_hash
                return deepcopy(previous[1])
            chunk = base64.b64decode(params["base64_chunk"], validate=True)
            assert hashlib.sha256(chunk).hexdigest() == params["chunk_hash"]
            buffer = self.uploads.setdefault(params["upload_id"], bytearray())
            assert params["offset"] == len(buffer)
            buffer.extend(chunk)
            completed = bool(params["final"])
            asset_id = None
            if completed:
                data = bytes(buffer)
                assert len(data) == params["total_size"]
                assert hashlib.sha256(data).hexdigest() == params["expected_hash"]
                asset_id = "asset-" + hashlib.sha256(data).hexdigest()[:40]
                self.assets[asset_id] = data
            result = {
                "upload_id": params["upload_id"],
                "accepted_bytes": len(buffer),
                "completed": completed,
                "asset_id": asset_id,
            }
            self.upload_results[replay_key] = (payload_hash, deepcopy(result))
            return result
        if method == "host.asset.upload.status/v1":
            self.assert_keys(params, {"upload_id", "expected_hash"})
            data = bytes(self.uploads[params["upload_id"]])
            assert hashlib.sha256(data).hexdigest() == params["expected_hash"]
            return {
                "accepted_bytes": len(data),
                "completed": True,
                "asset_id": "asset-" + hashlib.sha256(data).hexdigest()[:40],
            }
        if method == "host.model.invoke/v1":
            self.assert_keys(
                params,
                {
                    "operation_key", "invocation_id", "invocation_key",
                    "model_profile_revision_id", "request_asset_id", "replay_policy",
                },
            )
            if self.model_failures_remaining:
                self.model_failures_remaining -= 1
                raise RuntimeError("simulated model transport failure")
            model_request = json.loads(self.assets[params["request_asset_id"]].decode("utf-8"))
            output_schema = model_request["output_schema"]
            output = self.model_outputs[output_schema]
            response_asset_id, _ = self.seed_json("asset-model-" + hashlib.sha256(output_schema.encode()).hexdigest()[:16], output)
            return {
                "state": "received",
                "response_asset_id": response_asset_id,
                "receipt_id": "model-receipt-1",
                "uncertainty": None,
            }
        if method == "host.job.event/v1":
            self.assert_keys(params, {"operation_key", "event_type", "payload_asset_id", "local_seq"})
            self.event_calls.append(params)
            return {"accepted": True, "job_event_seq": self._next_seq()}
        if method == "host.checkpoint.commit/v1":
            self.assert_keys(params, {"operation_key", "checkpoint_asset_id"})
            self.checkpoint_calls.append(params)
            if self.block_checkpoint:
                self.block_checkpoint = False
                self.checkpoint_entered.set()
                assert self.checkpoint_release.wait(timeout=5)
            checkpoint = json.loads(self.assets[params["checkpoint_asset_id"]].decode("utf-8"))
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self._next_seq(),
            }
        if method == "host.candidate.stage/v1":
            self.assert_keys(params, {"operation_key", "result_bundle_asset_id", "input_snapshot_hash"})
            self.stage_calls.append(params)
            previous = self.logical_stage_results.get(params["operation_key"])
            if previous is not None:
                return deepcopy(previous)
            bundle = json.loads(self.assets[params["result_bundle_asset_id"]].decode("utf-8"))
            result = {
                "accepted": True,
                "staged_items": [
                    {
                        "item_id": item["item_id"],
                        "candidate_id": ("candidate-" + item["item_id"])[:128],
                        "stage_status": "created",
                        "publication_eligibility": "review_only",
                    }
                    for item in bundle["items"]
                ],
                "job_event_seq": self._next_seq(),
            }
            self.logical_stage_results[params["operation_key"]] = deepcopy(result)
            if self.interrupt_stage_after_accept:
                self.interrupt_stage_after_accept = False
                raise KeyboardInterrupt("simulated process stop after durable stage")
            return result
        if method == "host.job.complete/v1":
            self.assert_keys(
                params,
                {
                    "operation_key", "worker_run_id", "outcome", "result_bundle_asset_id",
                    "candidate_stage_operation_key", "terminal_detail_asset_id", "local_seq",
                },
            )
            self.completion_calls.append(params)
            sequence = self._next_seq()
            if self.completion_ack_mode == "transport_after_accept":
                raise RuntimeError("simulated lost terminal acknowledgement")
            if self.completion_ack_mode == "non_object":
                return None
            if self.completion_ack_mode == "stale_sequence":
                sequence -= 1
            result = {
                "accepted": True,
                "attempt_state": "failed" if self.completion_ack_mode == "wrong_state" else params["outcome"],
                "step_state": params["outcome"],
                "job_state": params["outcome"],
                "provenance_receipt_id": "receipt-other" if self.completion_ack_mode == "wrong_receipt" else "receipt-test",
                "job_event_seq": sequence,
                "core_event_high_water": sequence,
            }
            if self.completion_ack_mode == "missing_accepted":
                result.pop("accepted")
            elif self.completion_ack_mode == "unexpected_field":
                result["unexpected"] = True
            elif self.completion_ack_mode == "rejected":
                result["accepted"] = False
            return result
        raise AssertionError(f"unexpected Host method: {method}")


def common(capability: str, *, operation: str = "run", worker_run_id: str = "worker-1") -> dict:
    schema = {
        CAPABILITY_ATOM_EXTRACT: "analysis.book.atom.extract-request/v1",
        CAPABILITY_ATOM_MANUAL: "analysis.book.atom.manual-request/v1",
        CAPABILITY_CLAIM_GENERATE: "analysis.book.claim.generate-request/v1",
        CAPABILITY_REREVIEW: "analysis.book.rereview-request/v1",
    }[capability]
    return {
        "schema": schema,
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
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "1" * 64,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
    }


def atom_extract_request() -> dict:
    return {
        **common(CAPABILITY_ATOM_EXTRACT),
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "taxonomy_asset_id": "asset-taxonomy",
        "taxonomy_asset_hash": TAXONOMY_HASH,
        "model_profile_revision_id": "model-profile-1",
    }


def manual_request(*, operation: str = "run") -> dict:
    annotation_hash = hashlib.sha256("人工注释".encode("utf-8")).hexdigest()
    return {
        **common(CAPABILITY_ATOM_MANUAL, operation=operation),
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "taxonomy_asset_id": "asset-taxonomy",
        "taxonomy_asset_hash": TAXONOMY_HASH,
        "manual_annotation_asset_id": "asset-annotation",
        "manual_annotation_asset_hash": annotation_hash,
        "actor_id": "user-1",
        "atom_payload": atom_payload(manual=True),
    }


def seed_claim_assets(host: Host) -> tuple[dict, list[dict]]:
    atom = atom_payload()
    atom["provenance"] = trusted_atom_provenance()
    atom_id, atom_hash = host.seed_json("asset-accepted-atom", atom)
    frozen = claim_input()
    frozen["ordered_atoms"][0]["payload_hash"] = atom_hash
    _, parameters_hash = host.seed_json("asset-claim-input", frozen)
    accepted = [
        {
            "atom_id": "atom-accepted-1",
            "source_revision_id": "rev-1",
            "status": "accepted",
            "is_current": True,
            "acceptance_ordinal": 11,
            "payload_asset_id": atom_id,
            "payload_hash": atom_hash,
            "evidence_spans": [evidence()],
        }
    ]
    host.model_outputs["analysis.book.claim.generate-model-response/v1"] = {
        "schema": "analysis.book.claim.generate-model-response/v1",
        "items": [claim_payload(frozen)],
    }
    return frozen, accepted


def claim_request(host: Host) -> dict:
    _, accepted = seed_claim_assets(host)
    parameters_hash = hashlib.sha256(host.assets["asset-claim-input"]).hexdigest()
    return {
        **common(CAPABILITY_CLAIM_GENERATE),
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "parameters_asset_id": "asset-claim-input",
        "parameters_asset_hash": parameters_hash,
        "snapshot_parameters_asset_id": "asset-claim-input",
        "snapshot_asset_hashes": [
            {"asset_id": "asset-claim-input", "sha256": parameters_hash}
        ],
        "accepted_atoms": accepted,
        "model_profile_revision_id": "model-profile-1",
    }


def rereview_request(host: Host) -> dict:
    candidate_payload = canonical_bytes({**atom_payload(), "provenance": trusted_atom_provenance()})
    host.assets["asset-candidate-old"] = candidate_payload
    record = {
        "candidate_id": "candidate-successor-1",
        "candidate_kind": "book_atom",
        "status": "pending",
        "is_current": False,
        "payload_hash": hashlib.sha256(candidate_payload).hexdigest(),
        "payload_asset_id": "asset-candidate-old",
        "source_revision_id": "rev-1",
        "parent_candidate_id": "candidate-parent-1",
        "successor_candidate_id": "candidate-successor-1",
    }
    host.model_outputs["analysis.book.rereview-model-response/v1"] = {
        "schema": "analysis.book.rereview-model-response/v1",
        "items": [
            {
                "candidate_id": "candidate-successor-1",
                "severity": "warning",
                "code": "evidence_needs_review",
                "message": "需要人工复核",
                "recommendation": "compare_successor",
                "successor_candidate_id": "candidate-successor-1",
            }
        ],
    }
    return {
        **common(CAPABILITY_REREVIEW),
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "candidate_records": [record],
        "known_parent_candidate_ids": ["candidate-parent-1"],
        "allow_successor": True,
        "model_profile_revision_id": "model-profile-1",
    }


def test_automatic_atom_and_manual_atom_run_stage_only_candidates_with_public_receipts() -> None:
    host = Host()
    plugin = DonorAnalysisPlugin()
    automatic = plugin.run(atom_extract_request(), host)
    assert automatic is not None
    verify_result_bundle(automatic, snapshot_workspace_id="ws-1", snapshot_hash_value="1" * 64)
    assert automatic["contract_id"] == "candidate-batch/v1"
    assert automatic["items"][0]["mutation"]["payload_schema"] == "book-atom/v1"
    automatic_payload = json.loads(host.assets[automatic["items"][0]["payload_asset_id"]].decode("utf-8"))
    automatic_provenance = automatic_payload["provenance"]
    assert automatic_provenance["mode"] == "model"
    assert automatic_provenance["method"] == automatic_payload["analysis_method"]
    assert automatic_provenance["model"] == {
        "profile_revision_id": "model-profile-1", "receipt_id": "model-receipt-1"
    }
    assert automatic_provenance["skill"]["skill_id"] == "com.plotpilot.skill.donor.atomic-breakdown"
    assert automatic_provenance["plugin"]["capability_id"] == CAPABILITY_ATOM_EXTRACT
    assert automatic_provenance["run"]["worker_run_id"] == "worker-1"
    assert automatic_provenance["run_snapshot_hash"] == "1" * 64
    assert automatic_provenance["source_revision"] == {
        "workspace_id": "ws-1", "document_id": "doc-1", "revision_id": "rev-1",
        "canonical_text_hash": TEXT_HASH,
    }
    assert plugin.last_stage_response["staged_items"][0]["publication_eligibility"] == "review_only"
    committed_checkpoint = plugin.last_checkpoint["checkpoint"]
    verify_checkpoint(committed_checkpoint, expected_snapshot_hash="1" * 64)
    verify_provenance_receipt(plugin.last_receipt)
    assert plugin.last_receipt["run_snapshot_hash"] == "1" * 64
    assert plugin.last_receipt["model_receipt_ids"] == ["model-receipt-1"]
    assert host.completion_calls[-1]["outcome"] == "succeeded"

    manual = plugin.run(manual_request(), host)
    assert manual is not None and manual["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[manual["items"][0]["payload_asset_id"]].decode("utf-8"))
    assert payload["provenance"]["mode"] == "manual"
    assert payload["provenance"]["manual_annotation"] == {
        "actor_id": "user-1", "asset_id": "asset-annotation",
        "asset_hash": hashlib.sha256("人工注释".encode("utf-8")).hexdigest(),
    }
    assert payload["provenance"]["model"] is None
    assert plugin.last_receipt["model_receipt_ids"] == []


def test_manual_validate_is_deterministic_and_has_no_stage_or_terminal_side_effect() -> None:
    host = Host()
    plugin = DonorAnalysisPlugin()
    value = plugin.run(manual_request(operation="validate"), host)
    assert value is not None and value["contract_id"] == "candidate-batch/v1"
    assert host.stage_calls == []
    assert host.completion_calls == []
    assert plugin.last_stage_response is None and plugin.last_receipt is None


def test_claim_requires_exact_parameters_asset_and_current_accepted_atom_then_stages_claim() -> None:
    host = Host()
    request = claim_request(host)
    plugin = DonorAnalysisPlugin()
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    assert bundle["items"][0]["mutation"]["payload_schema"] == "book-claim/v1"
    assert plugin.last_receipt["model_receipt_ids"] == ["model-receipt-1"]
    assert len(host.stage_calls) == 1

    for mutation in (
        lambda r: r.update(parameters_asset_hash="0" * 64),
        lambda r: r["accepted_atoms"][0].update(is_current=False),
        lambda r: r["accepted_atoms"][0].update(status="superseded"),
        lambda r: r["accepted_atoms"][0].update(acceptance_ordinal=12),
        lambda r: r["accepted_atoms"][0].update(source_revision_id="rev-old"),
        lambda r: r["accepted_atoms"][0].update(payload_hash="f" * 64),
    ):
        bad_host = Host()
        bad = claim_request(bad_host)
        mutation(bad)
        bad_plugin = DonorAnalysisPlugin()
        failed = bad_plugin.run(bad, bad_host)
        assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
        assert failed["partial"] is True
        assert bad_host.stage_calls == []
        assert bad_host.completion_calls[-1]["outcome"] == "failed"


def test_claim_without_any_accepted_atom_fails_before_model_or_candidate_stage() -> None:
    host = Host()
    request = claim_request(host)
    request["accepted_atoms"] = []
    plugin = DonorAnalysisPlugin()
    failed = plugin.run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert not [call for call in host.calls if call[0] == "host.model.invoke/v1"]
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_rereview_is_diagnostic_only_and_preserves_existing_one_to_one_successor_lineage() -> None:
    host = Host()
    plugin = DonorAnalysisPlugin()
    bundle = plugin.run(rereview_request(host), host)
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["bundle_type"] == "diagnostic"
    assert host.stage_calls == []
    details = json.loads(host.assets[bundle["items"][0]["details_asset_id"]].decode("utf-8"))
    assert details["mutation_staged"] is False
    assert details["successor"] == {
        "mode": "existing_idempotent",
        "predecessor_candidate_id": "candidate-parent-1",
        "successor_candidate_id": "candidate-successor-1",
    }
    assert host.completion_calls[-1]["outcome"] == "succeeded"


def test_cancel_after_ready_checkpoint_then_resume_exact_binding_without_stale_cancel_or_model_replay() -> None:
    host = Host()
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = DonorAnalysisPlugin()
    request = atom_extract_request()
    result: dict[str, object] = {}

    def worker() -> None:
        result["bundle"] = plugin.run(request, host)

    thread = threading.Thread(target=worker)
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancelled = plugin.run({**request, "operation": "cancel"}, host)
    assert cancelled == {"accepted": True, "worker_run_id": "worker-1"}
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["bundle"] is None
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "cancelled"
    assert plugin.last_checkpoint is not None

    checkpoint_asset_id = host.checkpoint_calls[-1]["checkpoint_asset_id"]
    checkpoint_asset_hash = hashlib.sha256(host.assets[checkpoint_asset_id]).hexdigest()
    state_asset_id = plugin.last_checkpoint["state_asset_id"]
    state_asset_hash = plugin.last_checkpoint["state_hash"]
    resume = {
        **request,
        "operation": "resume",
        "resume_checkpoint_asset_id": checkpoint_asset_id,
        "resume_checkpoint_asset_hash": checkpoint_asset_hash,
        "resume_state_asset_id": state_asset_id,
        "resume_state_asset_hash": state_asset_hash,
    }
    model_calls_before = len([call for call in host.calls if call[0] == "host.model.invoke/v1"])
    resumed = plugin.run(resume, host)
    assert resumed is not None and resumed["contract_id"] == "candidate-batch/v1"
    assert len([call for call in host.calls if call[0] == "host.model.invoke/v1"]) == model_calls_before
    assert len(host.stage_calls) == 1
    assert [item["outcome"] for item in host.completion_calls[-2:]] == ["cancelled", "succeeded"]


def test_resume_tamper_fails_closed_without_candidate_stage() -> None:
    host = Host()
    plugin = DonorAnalysisPlugin()
    request = atom_extract_request()
    first = plugin.run(request, host)
    assert first is not None
    checkpoint_asset_id = host.checkpoint_calls[-1]["checkpoint_asset_id"]
    state_asset_id = plugin.last_checkpoint["state_asset_id"]
    state = json.loads(host.assets[state_asset_id].decode("utf-8"))
    state["binding_hash"] = "0" * 64
    bad_state_id, bad_state_hash = host.seed_json("asset-tampered-state", state)
    resume = {
        **request,
        "operation": "resume",
        "resume_checkpoint_asset_id": checkpoint_asset_id,
        "resume_checkpoint_asset_hash": hashlib.sha256(host.assets[checkpoint_asset_id]).hexdigest(),
        "resume_state_asset_id": bad_state_id,
        "resume_state_asset_hash": bad_state_hash,
    }
    stage_count = len(host.stage_calls)
    failed = plugin.run(resume, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert len(host.stage_calls) == stage_count
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_model_failure_recovery_and_candidate_stage_operation_key_are_idempotent() -> None:
    host = Host()
    host.model_failures_remaining = 1
    plugin = DonorAnalysisPlugin()
    request = atom_extract_request()
    failed = plugin.run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["partial"] is True and host.stage_calls == []
    recovered = plugin.run(request, host)
    assert recovered is not None and recovered["contract_id"] == "candidate-batch/v1"
    first_ids = [row["candidate_id"] for row in plugin.last_stage_response["staged_items"]]
    replayed = plugin.run(request, host)
    assert replayed == recovered
    second_ids = [row["candidate_id"] for row in plugin.last_stage_response["staged_items"]]
    assert second_ids == first_ids
    assert len(host.logical_stage_results) == 1


def test_unknown_request_field_and_strict_json_duplicate_parameters_fail_closed() -> None:
    host = Host()
    plugin = DonorAnalysisPlugin()
    bad = atom_extract_request()
    bad["unexpected"] = True
    failed = plugin.run(bad, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert host.stage_calls == []

    host = Host()
    request = claim_request(host)
    host.assets["asset-claim-input"] = b'{"schema":"claim-input/v1","schema":"claim-input/v1"}'
    request["parameters_asset_hash"] = hashlib.sha256(host.assets["asset-claim-input"]).hexdigest()
    plugin = DonorAnalysisPlugin()
    failed = plugin.run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert host.stage_calls == []


def test_f001_atoms_require_nonempty_exact_evidence_and_reject_untrusted_provenance_before_stage() -> None:
    for case in ("model-empty", "model-forged-provenance", "manual-empty"):
        host = Host()
        if case.startswith("model"):
            payload = atom_payload()
            if case == "model-empty":
                payload["evidence_spans"] = []
            else:
                payload["provenance"] = trusted_atom_provenance()
            host.model_outputs["analysis.book.atom.extract-model-response/v1"] = {
                "schema": "analysis.book.atom.extract-model-response/v1", "items": [payload]
            }
            request = atom_extract_request()
        else:
            request = manual_request()
            request["atom_payload"]["evidence_spans"] = []
        plugin = DonorAnalysisPlugin()
        failed = plugin.run(request, host)
        assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
        assert host.stage_calls == []
        if case == "manual-empty":
            assert not [call for call in host.calls if call[0] == "host.model.invoke/v1"]


def _claim_with_synchronized_evidence_mutation(mutate) -> tuple[Host, dict]:
    host = Host()
    request = claim_request(host)
    frozen = json.loads(host.assets["asset-claim-input"].decode("utf-8"))
    accepted = request["accepted_atoms"][0]
    payload_id = accepted["payload_asset_id"]
    payload = json.loads(host.assets[payload_id].decode("utf-8"))
    span = deepcopy(payload["evidence_spans"][0])
    mutate(span)
    payload["evidence_spans"] = [deepcopy(span)] if span is not None else []
    accepted["evidence_spans"] = deepcopy(payload["evidence_spans"])
    frozen["ordered_atoms"][0]["evidence_spans"] = deepcopy(payload["evidence_spans"])
    payload_bytes = canonical_bytes(payload)
    host.assets[payload_id] = payload_bytes
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    accepted["payload_hash"] = payload_hash
    frozen["ordered_atoms"][0]["payload_hash"] = payload_hash
    frozen_bytes = canonical_bytes(frozen)
    host.assets["asset-claim-input"] = frozen_bytes
    parameters_hash = hashlib.sha256(frozen_bytes).hexdigest()
    request["parameters_asset_hash"] = parameters_hash
    request["snapshot_asset_hashes"] = [
        {"asset_id": "asset-claim-input", "sha256": parameters_hash}
    ]
    return host, request


@pytest.mark.parametrize(
    "mutate",
    [
        lambda span: span.update(quote="ZZ", quote_hash=sha256_text("ZZ")),
        lambda span: span.update(start_codepoint=0),
        lambda span: span.update(node_id="node-missing"),
        lambda span: span.update(revision_id="rev-other"),
        lambda span: span.update(canonical_text_hash="e" * 64),
        lambda span: span.clear(),
    ],
    ids=["quote", "offset", "node", "revision", "canonical-hash", "empty-evidence"],
)
def test_f002_claim_revalidates_exact_accepted_atom_unicode_evidence_before_model_and_stage(mutate) -> None:
    def apply(span: dict) -> None:
        mutate(span)

    host, request = _claim_with_synchronized_evidence_mutation(apply)
    if not request["accepted_atoms"][0]["evidence_spans"] or request["accepted_atoms"][0]["evidence_spans"][0] == {}:
        request["accepted_atoms"][0]["evidence_spans"] = []
        frozen = json.loads(host.assets["asset-claim-input"].decode("utf-8"))
        frozen["ordered_atoms"][0]["evidence_spans"] = []
        payload_id = request["accepted_atoms"][0]["payload_asset_id"]
        payload = json.loads(host.assets[payload_id].decode("utf-8"))
        payload["evidence_spans"] = []
        payload_bytes = canonical_bytes(payload)
        host.assets[payload_id] = payload_bytes
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        request["accepted_atoms"][0]["payload_hash"] = payload_hash
        frozen["ordered_atoms"][0]["payload_hash"] = payload_hash
        frozen_bytes = canonical_bytes(frozen)
        host.assets["asset-claim-input"] = frozen_bytes
        parameters_hash = hashlib.sha256(frozen_bytes).hexdigest()
        request["parameters_asset_hash"] = parameters_hash
        request["snapshot_asset_hashes"] = [{"asset_id": "asset-claim-input", "sha256": parameters_hash}]
    plugin = DonorAnalysisPlugin()
    failed = plugin.run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert not [call for call in host.calls if call[0] == "host.model.invoke/v1"]
    assert host.stage_calls == []


def _two_record_rereview(host: Host) -> dict:
    request = rereview_request(host)
    payload = canonical_bytes({**atom_payload(), "provenance": trusted_atom_provenance()})
    host.assets["asset-candidate-two"] = payload
    request["known_parent_candidate_ids"].append("candidate-parent-2")
    request["candidate_records"].append({
        "candidate_id": "candidate-successor-2", "candidate_kind": "book_atom",
        "status": "pending", "is_current": False,
        "payload_hash": hashlib.sha256(payload).hexdigest(),
        "payload_asset_id": "asset-candidate-two", "source_revision_id": "rev-1",
        "parent_candidate_id": "candidate-parent-2",
        "successor_candidate_id": "candidate-successor-2",
    })
    diagnostics = host.model_outputs["analysis.book.rereview-model-response/v1"]["items"]
    diagnostics.append({
        "candidate_id": "candidate-successor-2", "severity": "info",
        "code": "evidence_ok", "message": "完整", "recommendation": "retain",
        "successor_candidate_id": "candidate-successor-2",
    })
    return request


@pytest.mark.parametrize("variant", ["missing", "duplicate", "unknown", "reordered"])
def test_f003_rereview_requires_exact_ordered_one_to_one_diagnostics(variant: str) -> None:
    host = Host()
    request = _two_record_rereview(host)
    items = host.model_outputs["analysis.book.rereview-model-response/v1"]["items"]
    if variant == "missing":
        items.pop()
    elif variant == "duplicate":
        items[1]["candidate_id"] = items[0]["candidate_id"]
    elif variant == "unknown":
        items[1]["candidate_id"] = "candidate-unknown"
    else:
        items.reverse()
    plugin = DonorAnalysisPlugin()
    failed = plugin.run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_f003_rereview_valid_multi_record_order_remains_diagnostic_only() -> None:
    host = Host()
    bundle = DonorAnalysisPlugin().run(_two_record_rereview(host), host)
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert [item["code"] for item in bundle["items"]] == ["evidence_needs_review", "evidence_ok"]
    assert host.stage_calls == []


@pytest.mark.parametrize("mode", ["stale_sequence", "wrong_state", "wrong_receipt", "transport_after_accept"])
def test_f004_terminal_completion_is_dispatched_at_most_once_even_for_bad_ack(mode: str) -> None:
    host = Host()
    host.completion_ack_mode = mode
    with pytest.raises(TerminalContractError):
        DonorAnalysisPlugin().run(atom_extract_request(), host)
    assert len(host.completion_calls) == 1
    assert host.completion_calls[0]["outcome"] == "succeeded"


def _resume_request(request: dict, plugin: DonorAnalysisPlugin, host: Host) -> dict:
    checkpoint_asset_id = host.checkpoint_calls[-1]["checkpoint_asset_id"]
    return {
        **request, "operation": "resume",
        "resume_checkpoint_asset_id": checkpoint_asset_id,
        "resume_checkpoint_asset_hash": hashlib.sha256(host.assets[checkpoint_asset_id]).hexdigest(),
        "resume_state_asset_id": plugin.last_checkpoint["state_asset_id"],
        "resume_state_asset_hash": plugin.last_checkpoint["state_hash"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "job-other"), ("step_id", "step-other"),
        ("source_attempt_id", "attempt-other"), ("lease_epoch", 99),
        ("created_at", "2026-08-29T00:00:00Z"),
        ("run_snapshot_hash", "2" * 64),
    ],
)
def test_f005_resume_rejects_each_exact_attempt_fence_change(field: str, value) -> None:
    host = Host()
    request = atom_extract_request()
    request["checkpoint_ids"] = ["checkpoint-1", "checkpoint-2"]
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume = _resume_request(request, plugin, host)
    checkpoint = json.loads(host.assets[resume["resume_checkpoint_asset_id"]].decode("utf-8"))
    checkpoint[field] = value
    checkpoint["checkpoint_hash"] = hash_json("checkpoint/v1", {k: v for k, v in checkpoint.items() if k != "checkpoint_hash"})
    asset_id, asset_hash = host.seed_json("asset-tampered-checkpoint-" + field, checkpoint)
    resume["resume_checkpoint_asset_id"] = asset_id
    resume["resume_checkpoint_asset_hash"] = asset_hash
    stage_count = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert len(host.stage_calls) == stage_count


@pytest.mark.parametrize(("checkpoint_seq", "checkpoint_id"), [(2, "checkpoint-2"), (1, "checkpoint-2")])
def test_f005_resume_rejects_checkpoint_sequence_or_id_position_change(checkpoint_seq: int, checkpoint_id: str) -> None:
    host = Host()
    request = atom_extract_request()
    request["checkpoint_ids"] = ["checkpoint-1", "checkpoint-2"]
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume = _resume_request(request, plugin, host)
    checkpoint = json.loads(host.assets[resume["resume_checkpoint_asset_id"]].decode("utf-8"))
    checkpoint["checkpoint_seq"] = checkpoint_seq
    checkpoint["checkpoint_id"] = checkpoint_id
    checkpoint["checkpoint_hash"] = hash_json("checkpoint/v1", {k: v for k, v in checkpoint.items() if k != "checkpoint_hash"})
    asset_id, asset_hash = host.seed_json("asset-tampered-checkpoint-position", checkpoint)
    resume["resume_checkpoint_asset_id"] = asset_id
    resume["resume_checkpoint_asset_hash"] = asset_hash
    stage_count = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert len(host.stage_calls) == stage_count


def test_f005_resume_rejects_synchronized_state_and_bundle_snapshot_tamper() -> None:
    host = Host()
    request = atom_extract_request()
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume = _resume_request(request, plugin, host)

    state = json.loads(host.assets[resume["resume_state_asset_id"]].decode("utf-8"))
    bundle = json.loads(host.assets[state["result_bundle_asset_id"]].decode("utf-8"))
    bundle["input_snapshot_hash"] = "2" * 64
    bundle_id, bundle_hash = host.seed_json("asset-tampered-resume-bundle", bundle)
    state["result_bundle_asset_id"] = bundle_id
    state["result_bundle_hash"] = bundle_hash
    state_id, state_hash = host.seed_json("asset-tampered-resume-state-bundle", state)

    checkpoint = json.loads(host.assets[resume["resume_checkpoint_asset_id"]].decode("utf-8"))
    checkpoint["state_asset_id"] = state_id
    checkpoint["unit_set_hash"] = state_hash
    checkpoint["checkpoint_hash"] = hash_json(
        "checkpoint/v1", {key: value for key, value in checkpoint.items() if key != "checkpoint_hash"}
    )
    checkpoint_id, checkpoint_hash = host.seed_json("asset-tampered-resume-checkpoint-bundle", checkpoint)
    resume.update({
        "resume_checkpoint_asset_id": checkpoint_id,
        "resume_checkpoint_asset_hash": checkpoint_hash,
        "resume_state_asset_id": state_id,
        "resume_state_asset_hash": state_hash,
    })
    stage_count = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert len(host.stage_calls) == stage_count


def test_f006_stage_stop_resume_reuses_exact_operation_key_and_model_provenance() -> None:
    host = Host()
    host.interrupt_stage_after_accept = True
    request = atom_extract_request()
    first_plugin = DonorAnalysisPlugin()
    with pytest.raises(KeyboardInterrupt):
        first_plugin.run(request, host)
    assert len(host.stage_calls) == 1 and len(host.logical_stage_results) == 1
    resume = _resume_request(request, first_plugin, host)
    state = json.loads(host.assets[resume["resume_state_asset_id"]].decode("utf-8"))
    expected_bundle = json.loads(
        host.assets[state["result_bundle_asset_id"]].decode("utf-8")
    )
    model_before = _g2_call_count(host, "host.model.invoke/v1")
    resumed_plugin = DonorAnalysisPlugin()
    bundle = resumed_plugin.run(resume, host)
    assert bundle == expected_bundle
    assert bundle["contract_id"] == "candidate-batch/v1"
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == 2
    assert host.stage_calls[0]["operation_key"] == host.stage_calls[1]["operation_key"]
    assert len(host.logical_stage_results) == 1
    assert resumed_plugin.last_receipt["model_receipt_ids"] == ["model-receipt-1"]
    assert resumed_plugin.last_receipt["skill_chain_result_refs"] == []
    assert bundle["skill_chain_result_refs"] == []
    assert host.completion_calls[-1]["outcome"] == "succeeded"


def _g2_call_count(host: Host, method: str) -> int:
    return len([call for call in host.calls if call[0] == method])


def _g2_failure_details(host: Host, bundle: dict) -> dict:
    assert bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["partial"] is True
    assert len(bundle["items"]) == 1
    return json.loads(host.assets[bundle["items"][0]["details_asset_id"]].decode("utf-8"))


def _g2_ready_resume(
    host: Host,
    request: dict,
    plugin: DonorAnalysisPlugin,
) -> tuple[dict, dict, dict, dict]:
    resume = _resume_request(request, plugin, host)
    checkpoint = json.loads(host.assets[resume["resume_checkpoint_asset_id"]].decode("utf-8"))
    state = json.loads(host.assets[resume["resume_state_asset_id"]].decode("utf-8"))
    bundle = json.loads(host.assets[state["result_bundle_asset_id"]].decode("utf-8"))
    return resume, checkpoint, state, bundle


def _g2_rebind_state_and_checkpoint(
    host: Host,
    resume: dict,
    checkpoint: dict,
    state: dict,
    *,
    suffix: str,
) -> None:
    state_id, state_hash = host.seed_json(f"asset-g2-state-{suffix}", state)
    checkpoint["state_asset_id"] = state_id
    checkpoint["unit_set_hash"] = state_hash
    checkpoint["checkpoint_hash"] = hash_json(
        "checkpoint/v1",
        {key: value for key, value in checkpoint.items() if key != "checkpoint_hash"},
    )
    checkpoint_id, checkpoint_hash = host.seed_json(
        f"asset-g2-checkpoint-{suffix}", checkpoint
    )
    resume.update(
        {
            "resume_checkpoint_asset_id": checkpoint_id,
            "resume_checkpoint_asset_hash": checkpoint_hash,
            "resume_state_asset_id": state_id,
            "resume_state_asset_hash": state_hash,
        }
    )


def _g2_derived_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256((prefix + "\n" + "\n".join(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:48]}"


def _g2_diagnostic_bundle_provenance_hash(host: Host, bundle: dict) -> str:
    projection = {
        "schema": "donor-analysis-bundle-provenance/v1",
        "producer": deepcopy(bundle["producer"]),
        "input_snapshot_hash": bundle["input_snapshot_hash"],
        "provenance_receipt_id": bundle["provenance_receipt_id"],
        "items": [],
    }
    for item in bundle["items"]:
        details = json.loads(host.assets[item["details_asset_id"]].decode("utf-8"))
        projection["items"].append(
            {
                "item_id": item["item_id"],
                "source_refs": deepcopy(item["source_refs"]),
                "status": item["status"],
                "details_asset_id": item["details_asset_id"],
                "details_hash": item["details_hash"],
                "details": details,
            }
        )
    return hash_json("donor-analysis-bundle-provenance/v1", projection)


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
def test_g2_runtime_binds_exact_complete_atom_provenance_and_unicode_evidence(
    manual: bool,
) -> None:
    host = Host()
    request = manual_request() if manual else atom_extract_request()
    plugin = DonorAnalysisPlugin()
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[bundle["items"][0]["payload_asset_id"]].decode("utf-8"))
    span = payload["evidence_spans"][0]
    provenance = payload["provenance"]

    assert span["quote"] == "😀e\u0301" == TEXT[1:4]
    assert span["start_codepoint"] == 1 and span["end_codepoint"] == 4
    assert span["quote_hash"] == sha256_text(span["quote"])
    assert provenance["plugin"] == {
        "plugin_id": "com.plotpilot.novelagent.donor-analysis",
        "package_hash": plugin.package_hash,
        "release_id": plugin.release_id,
        "capability_id": request["capability_id"],
    }
    assert provenance["run"] == {
        "job_id": request["job_id"],
        "step_id": request["step_id"],
        "attempt_id": request["attempt_id"],
        "worker_run_id": request["worker_run_id"],
        "lease_epoch": request["lease_epoch"],
        "provenance_receipt_id": request["provenance_receipt_id"],
    }
    assert provenance["run_snapshot_hash"] == request["run_snapshot_hash"]
    assert provenance["source_revision"] == {
        "workspace_id": request["workspace_id"],
        "document_id": request["document_id"],
        "revision_id": request["source_revision_id"],
        "canonical_text_hash": request["canonical_text_hash"],
    }
    assert provenance["created_at"] == request["created_at"]
    if manual:
        assert provenance["mode"] == "manual"
        assert provenance["model"] is None
        assert provenance["manual_annotation"] == {
            "actor_id": request["actor_id"],
            "asset_id": request["manual_annotation_asset_id"],
            "asset_hash": request["manual_annotation_asset_hash"],
        }
        assert _g2_call_count(host, "host.model.invoke/v1") == 0
    else:
        assert provenance["mode"] == "model"
        assert provenance["model"] == {
            "profile_revision_id": request["model_profile_revision_id"],
            "receipt_id": "model-receipt-1",
        }
        assert provenance["manual_annotation"] is None
    assert len(host.stage_calls) == 1


@pytest.mark.parametrize(
    ("manual", "variant"),
    [
        (False, "empty"), (False, "quote"), (False, "offset"),
        (False, "node"), (False, "revision"), (False, "canonical-hash"),
        (True, "empty"), (True, "quote"), (True, "offset"),
        (True, "node"), (True, "revision"), (True, "canonical-hash"),
    ],
)
def test_g2_runtime_rejects_atom_evidence_drift_before_candidate_stage(
    manual: bool,
    variant: str,
) -> None:
    host = Host()
    request = manual_request() if manual else atom_extract_request()
    payload = request["atom_payload"] if manual else host.model_outputs[
        "analysis.book.atom.extract-model-response/v1"
    ]["items"][0]
    span = payload["evidence_spans"][0]
    if variant == "empty":
        payload["evidence_spans"] = []
    elif variant == "quote":
        span.update(quote="😀é", quote_hash=sha256_text("😀é"))
    elif variant == "offset":
        span.update(start_codepoint=0)
    elif variant == "node":
        span.update(node_id="node-missing")
    elif variant == "revision":
        span.update(revision_id="rev-other")
    else:
        span.update(canonical_text_hash="e" * 64)

    failed = DonorAnalysisPlugin().run(request, host)
    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "ATOM_INVALID"
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"
    assert _g2_call_count(host, "host.model.invoke/v1") == (0 if manual else 1)


@pytest.mark.parametrize(
    "variant",
    [
        "quote", "quote-hash", "offset", "node", "revision", "canonical-hash",
        "empty", "not-current", "not-accepted", "atom-id", "payload-hash",
        "acceptance-ordinal",
    ],
)
def test_g2_claim_rejects_canonical_or_accepted_atom_drift_before_model_and_stage(
    variant: str,
) -> None:
    if variant in {"quote", "quote-hash", "offset", "node", "revision", "canonical-hash", "empty"}:
        def mutate(span: dict) -> None:
            if variant == "quote":
                span.update(quote="ZZ", quote_hash=sha256_text("ZZ"))
            elif variant == "quote-hash":
                span.update(quote_hash="e" * 64)
            elif variant == "offset":
                span.update(start_codepoint=0)
            elif variant == "node":
                span.update(node_id="node-missing")
            elif variant == "revision":
                span.update(revision_id="rev-other")
            elif variant == "canonical-hash":
                span.update(canonical_text_hash="e" * 64)
            else:
                span.clear()

        host, request = _claim_with_synchronized_evidence_mutation(mutate)
        if variant == "empty":
            request["accepted_atoms"][0]["evidence_spans"] = []
            frozen = json.loads(host.assets["asset-claim-input"].decode("utf-8"))
            frozen["ordered_atoms"][0]["evidence_spans"] = []
            payload_id = request["accepted_atoms"][0]["payload_asset_id"]
            payload = json.loads(host.assets[payload_id].decode("utf-8"))
            payload["evidence_spans"] = []
            payload_id, payload_hash = host.seed_json("asset-g2-empty-accepted-atom", payload)
            request["accepted_atoms"][0].update(
                payload_asset_id=payload_id,
                payload_hash=payload_hash,
            )
            frozen["ordered_atoms"][0]["payload_hash"] = payload_hash
            _, parameters_hash = host.seed_json("asset-claim-input", frozen)
            request["parameters_asset_hash"] = parameters_hash
            request["snapshot_asset_hashes"] = [
                {"asset_id": "asset-claim-input", "sha256": parameters_hash}
            ]
    else:
        host = Host()
        request = claim_request(host)
        accepted = request["accepted_atoms"][0]
        if variant == "not-current":
            accepted["is_current"] = False
        elif variant == "not-accepted":
            accepted["status"] = "superseded"
        elif variant == "atom-id":
            accepted["atom_id"] = "atom-other"
        elif variant == "payload-hash":
            accepted["payload_hash"] = "e" * 64
        else:
            accepted["acceptance_ordinal"] = 99

    failed = DonorAnalysisPlugin().run(request, host)
    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "CLAIM_AUTHORITY_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == 0
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


@pytest.mark.parametrize(
    "variant",
    ["null-successor", "missing", "duplicate", "unknown", "reordered"],
)
def test_g2_rereview_rejects_nonexact_ordered_lineage_from_model(variant: str) -> None:
    host = Host()
    request = _two_record_rereview(host)
    items = host.model_outputs["analysis.book.rereview-model-response/v1"]["items"]
    if variant == "null-successor":
        items[0]["successor_candidate_id"] = None
    elif variant == "missing":
        items.pop()
    elif variant == "duplicate":
        items[1] = deepcopy(items[0])
    elif variant == "unknown":
        items[1]["candidate_id"] = "candidate-unknown"
    else:
        items.reverse()

    failed = DonorAnalysisPlugin().run(request, host)
    assert failed is not None
    details = _g2_failure_details(host, failed)
    assert details["code"] in {"MODEL_OUTPUT_INVALID", "REREVIEW_LINEAGE_INVALID"}
    assert _g2_call_count(host, "host.model.invoke/v1") == 1
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


@pytest.mark.parametrize(
    "mode",
    [
        "stale_sequence", "wrong_state", "wrong_receipt", "transport_after_accept",
        "non_object", "missing_accepted", "unexpected_field", "rejected",
    ],
)
def test_g2_terminal_completion_is_sent_exactly_once_for_any_bad_ack(mode: str) -> None:
    host = Host()
    host.completion_ack_mode = mode
    with pytest.raises(TerminalContractError):
        DonorAnalysisPlugin().run(atom_extract_request(), host)
    assert len(host.completion_calls) == 1
    assert host.completion_calls[0]["outcome"] == "succeeded"


@pytest.mark.parametrize(
    "variant",
    [
        "checkpoint-job", "checkpoint-step", "checkpoint-attempt", "checkpoint-lease",
        "checkpoint-created", "checkpoint-sequence", "checkpoint-id",
        "checkpoint-run-snapshot", "state-binding", "state-plugin", "state-package",
        "state-release", "state-capability", "state-job", "state-step", "state-attempt",
        "state-worker", "state-lease", "state-created", "state-receipt",
        "state-workspace", "state-document", "state-revision", "state-run-snapshot",
        "state-bundle-provenance", "state-skill-provenance",
        "producer-job", "producer-step", "producer-attempt",
        "producer-lease", "producer-capability", "producer-release", "producer-plugin",
        "bundle-receipt", "bundle-run-snapshot",
    ],
)
def test_g2_resume_rejects_every_checkpoint_state_and_bundle_producer_mismatch(
    variant: str,
) -> None:
    host = Host()
    request = rereview_request(host)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(host, request, plugin)

    if variant == "checkpoint-job":
        checkpoint["job_id"] = "job-other"
    elif variant == "checkpoint-step":
        checkpoint["step_id"] = "step-other"
    elif variant == "checkpoint-attempt":
        checkpoint["source_attempt_id"] = "attempt-other"
    elif variant == "checkpoint-lease":
        checkpoint["lease_epoch"] = 2
    elif variant == "checkpoint-created":
        checkpoint["created_at"] = "2026-08-31T00:00:00Z"
    elif variant == "checkpoint-sequence":
        checkpoint["checkpoint_seq"] = 2
    elif variant == "checkpoint-id":
        checkpoint["checkpoint_id"] = "checkpoint-other"
    elif variant == "checkpoint-run-snapshot":
        checkpoint["run_snapshot_hash"] = "2" * 64
    elif variant == "state-capability":
        state["capability_id"] = CAPABILITY_CLAIM_GENERATE
    elif variant == "state-binding":
        state["binding_hash"] = "2" * 64
    elif variant == "state-plugin":
        state["plugin_id"] = "com.plotpilot.novelagent.other"
    elif variant == "state-package":
        state["package_hash"] = "2" * 64
    elif variant == "state-release":
        state["release_id"] = "2" * 64
    elif variant == "state-job":
        state["job_id"] = "job-other"
    elif variant == "state-step":
        state["step_id"] = "step-other"
    elif variant == "state-attempt":
        state["attempt_id"] = "attempt-other"
    elif variant == "state-worker":
        state["worker_run_id"] = "worker-other"
    elif variant == "state-lease":
        state["lease_epoch"] = 2
    elif variant == "state-created":
        state["created_at"] = "2026-08-31T00:00:00Z"
    elif variant == "state-receipt":
        state["provenance_receipt_id"] = "receipt-other"
    elif variant == "state-workspace":
        state["workspace_id"] = "ws-other"
    elif variant == "state-document":
        state["document_id"] = "doc-other"
    elif variant == "state-revision":
        state["source_revision_id"] = "rev-other"
    elif variant == "state-run-snapshot":
        state["run_snapshot_hash"] = "2" * 64
    elif variant == "state-bundle-provenance":
        state["bundle_provenance_hash"] = "2" * 64
    elif variant == "state-skill-provenance":
        state["skill_chain_result_refs"] = [
            {
                "schema": "skill-chain-ref/v1",
                "chain_result_id": "chain-other",
                "asset_id": None,
                "asset_hash": None,
                "result_bundle_id": None,
                "result_item_id": None,
                "stream_id": None,
                "acked_prefix_hash": None,
            }
        ]
    elif variant == "bundle-receipt":
        bundle["provenance_receipt_id"] = "receipt-other"
    elif variant == "bundle-run-snapshot":
        bundle["input_snapshot_hash"] = "2" * 64
    else:
        field = variant.removeprefix("producer-")
        if field == "lease":
            field = "lease_epoch"
        replacements = {
            "job": "job-other",
            "step": "step-other",
            "attempt": "attempt-other",
            "lease_epoch": 2,
            "capability": CAPABILITY_CLAIM_GENERATE,
            "release": "2" * 64,
            "plugin": "com.plotpilot.novelagent.other",
        }
        producer_field = {
            "job": "job_id", "step": "step_id", "attempt": "attempt_id",
            "capability": "capability_id", "release": "release_id", "plugin": "plugin_id",
        }.get(field, field)
        bundle["producer"][producer_field] = replacements[field]

    if variant.startswith("producer-") or variant.startswith("bundle-"):
        bundle_id, bundle_hash = host.seed_json(f"asset-g2-bundle-{variant}", bundle)
        state["result_bundle_asset_id"] = bundle_id
        state["result_bundle_hash"] = bundle_hash
        state["bundle_provenance_hash"] = _g2_diagnostic_bundle_provenance_hash(
            host, bundle
        )
    _g2_rebind_state_and_checkpoint(
        host, resume, checkpoint, state, suffix=variant
    )

    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "RESUME_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_g2_identical_bundle_bytes_under_another_asset_id_reuse_logical_stage_key() -> None:
    host = Host()
    request = atom_extract_request()
    plugin = DonorAnalysisPlugin()
    original = plugin.run(request, host)
    assert original is not None and len(host.stage_calls) == 1
    original_key = host.stage_calls[0]["operation_key"]
    resume, checkpoint, state, _bundle = _g2_ready_resume(host, request, plugin)
    original_bundle_id = state["result_bundle_asset_id"]
    alternate_bundle_id = "asset-g2-identical-bundle-copy"
    host.assets[alternate_bundle_id] = bytes(host.assets[original_bundle_id])
    state["result_bundle_asset_id"] = alternate_bundle_id
    _g2_rebind_state_and_checkpoint(
        host, resume, checkpoint, state, suffix="identical-bundle-copy"
    )

    model_before = _g2_call_count(host, "host.model.invoke/v1")
    resumed = DonorAnalysisPlugin().run(resume, host)
    assert resumed == original
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == 2
    assert host.stage_calls[1]["operation_key"] == original_key
    assert len(host.logical_stage_results) == 1
    assert host.completion_calls[-1]["outcome"] == "succeeded"


@pytest.mark.parametrize("capability", ["atom", "claim"])
def test_g2_resume_state_model_receipts_equal_payload_recomputed_provenance(
    capability: str,
) -> None:
    host = Host()
    request = atom_extract_request() if capability == "atom" else claim_request(host)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, _bundle = _g2_ready_resume(host, request, plugin)
    assert state["model_receipt_ids"] == ["model-receipt-1"]
    state["model_receipt_ids"] = ["model-receipt-other"]
    _g2_rebind_state_and_checkpoint(
        host, resume, checkpoint, state, suffix=f"{capability}-receipt-drift"
    )

    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None
    details = _g2_failure_details(host, failed)
    assert details["code"] == "RESUME_INVALID"
    assert "provenance" in details["message"].lower() or "receipt" in details["message"].lower()
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_g2_resume_rejects_synchronized_atom_payload_package_provenance_drift() -> None:
    host = Host()
    request = atom_extract_request()
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(host, request, plugin)

    item = bundle["items"][0]
    payload = json.loads(host.assets[item["payload_asset_id"]].decode("utf-8"))
    payload["provenance"]["plugin"]["package_hash"] = "2" * 64
    payload_id, payload_hash = host.seed_json("asset-g2-package-drift-atom", payload)
    item["payload_asset_id"] = payload_id
    item["mutation"]["payload_hash"] = payload_hash
    request_hash = hash_json(request["capability_id"] + "-request/v1", request)
    item_id = _g2_derived_id("candidate", request_hash, "0", payload_hash)
    entity_id = "book-atom:" + item_id.split(":", 1)[1]
    item["item_id"] = item_id
    item["target"]["entity_id"] = entity_id
    item["write_set"][0]["entity_id"] = entity_id
    bundle_id, bundle_hash = host.seed_json("asset-g2-package-drift-bundle", bundle)
    state["result_bundle_asset_id"] = bundle_id
    state["result_bundle_hash"] = bundle_hash
    state["candidate_stage_operation_key"] = _g2_derived_id(
        "candidate-stage", state["binding_hash"], bundle_hash
    )
    _g2_rebind_state_and_checkpoint(
        host, resume, checkpoint, state, suffix="package-provenance-drift"
    )

    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)
    failed = DonorAnalysisPlugin().run(resume, host)
    assert failed is not None
    details = _g2_failure_details(host, failed)
    assert details["code"] == "RESUME_INVALID"
    assert "stage operation" not in details["message"].lower()
    assert "package" in details["message"].lower() or "provenance" in details["message"].lower()
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_g2_legal_cancel_then_resume_has_no_stale_cancellation() -> None:
    host = Host()
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = DonorAnalysisPlugin()
    request = atom_extract_request()
    result: dict[str, object] = {}

    thread = threading.Thread(target=lambda: result.update(bundle=plugin.run(request, host)))
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    assert plugin.run({**request, "operation": "cancel"}, host) == {
        "accepted": True,
        "worker_run_id": request["worker_run_id"],
    }
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["bundle"] is None
    assert host.completion_calls[-1]["outcome"] == "cancelled"

    resume = _resume_request(request, plugin, host)
    model_before = _g2_call_count(host, "host.model.invoke/v1")
    resumed = DonorAnalysisPlugin().run(resume, host)
    assert resumed is not None and resumed["contract_id"] == "candidate-batch/v1"
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == 1
    assert len(host.logical_stage_results) == 1
    assert resumed["skill_chain_result_refs"] == []
    assert host.completion_calls[-1]["outcome"] == "succeeded"
    assert [call["outcome"] for call in host.completion_calls[-2:]] == [
        "cancelled", "succeeded",
    ]


def _g2_resume_binding_hash(request: dict) -> str:
    projection = {
        key: deepcopy(value)
        for key, value in request.items()
        if key not in {
            "resume_checkpoint_asset_id", "resume_checkpoint_asset_hash",
            "resume_state_asset_id", "resume_state_asset_hash",
        }
    }
    projection["operation"] = "run"
    return hash_json(request["capability_id"] + "-binding/v1", projection)


def _g2_claim_bundle_provenance_hash(host: Host, bundle: dict) -> str:
    projection = {
        "schema": "donor-analysis-bundle-provenance/v1",
        "producer": deepcopy(bundle["producer"]),
        "input_snapshot_hash": bundle["input_snapshot_hash"],
        "provenance_receipt_id": bundle["provenance_receipt_id"],
        "items": [],
    }
    for item in bundle["items"]:
        payload = json.loads(host.assets[item["payload_asset_id"]].decode("utf-8"))
        projection["items"].append(
            {
                "item_id": item["item_id"],
                "source_refs": deepcopy(item["source_refs"]),
                "status": item["status"],
                "payload_asset_id": item["payload_asset_id"],
                "payload_hash": item["mutation"]["payload_hash"],
                "payload_schema": item["mutation"]["payload_schema"],
                "base": deepcopy(item["base"]),
                "write_set": deepcopy(item["write_set"]),
                "parent_candidate_ids": deepcopy(item["parent_candidate_ids"]),
                "payload_provenance": {
                    "ordered_atom_ids": deepcopy(payload["ordered_atom_ids"]),
                    "evidence_spans": deepcopy(payload["evidence_spans"]),
                    "analysis_method": deepcopy(payload["analysis_method"]),
                },
            }
        )
    return hash_json("donor-analysis-bundle-provenance/v1", projection)


def _g2_rebind_result_bundle(
    host: Host,
    resume: dict,
    checkpoint: dict,
    state: dict,
    bundle: dict,
    *,
    provenance_hash: str,
    suffix: str,
) -> None:
    bundle_id, bundle_hash = host.seed_json(f"asset-g2-rebound-bundle-{suffix}", bundle)
    state["result_bundle_asset_id"] = bundle_id
    state["result_bundle_hash"] = bundle_hash
    state["bundle_provenance_hash"] = provenance_hash
    state["binding_hash"] = _g2_resume_binding_hash(resume)
    if state["candidate_stage_operation_key"] is not None:
        state["candidate_stage_operation_key"] = _g2_derived_id(
            "candidate-stage", state["binding_hash"], bundle_hash
        )
    _g2_rebind_state_and_checkpoint(
        host, resume, checkpoint, state, suffix=suffix
    )


def _g2_retarget_rereview_detail(
    host: Host,
    item: dict,
    record: dict,
    *,
    suffix: str,
) -> None:
    details = json.loads(host.assets[item["details_asset_id"]].decode("utf-8"))
    predecessor = record["parent_candidate_id"]
    successor = record["successor_candidate_id"]
    details["candidate_id"] = record["candidate_id"]
    details["lineage"] = {
        "predecessor_candidate_id": predecessor,
        "successor_candidate_id": successor,
    }
    details["successor"] = None if successor is None else {
        "mode": "existing_idempotent",
        "predecessor_candidate_id": predecessor,
        "successor_candidate_id": successor,
    }
    details_id, details_hash = host.seed_json(
        f"asset-g2-rebound-rereview-detail-{suffix}", details
    )
    item["details_asset_id"] = details_id
    item["details_hash"] = details_hash


def test_g2_resume_rereview_legal_control_preserves_ordered_diagnostics() -> None:
    host = Host()
    request = _two_record_rereview(host)
    plugin = DonorAnalysisPlugin()
    original = plugin.run(request, host)
    assert original is not None and original["contract_id"] == "diagnostic-bundle/v1"
    expected_ids = [record["candidate_id"] for record in request["candidate_records"]]
    assert [
        json.loads(host.assets[item["details_asset_id"]].decode("utf-8"))["candidate_id"]
        for item in original["items"]
    ] == expected_ids
    resume = _resume_request(request, plugin, host)
    model_before = _g2_call_count(host, "host.model.invoke/v1")

    resumed = DonorAnalysisPlugin().run(resume, host)

    assert resumed == original
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "succeeded"


@pytest.mark.parametrize("variant", ["omit", "duplicate", "reorder", "replace"])
def test_g2_resume_rereview_rejects_rebound_diagnostic_projection(
    variant: str,
) -> None:
    host = Host()
    request = _two_record_rereview(host)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(host, request, plugin)

    if variant == "omit":
        bundle["items"].pop()
    elif variant == "duplicate":
        _g2_retarget_rereview_detail(
            host,
            bundle["items"][1],
            request["candidate_records"][0],
            suffix="duplicate",
        )
    elif variant == "reorder":
        bundle["items"].reverse()
    else:
        bundle["items"] = [deepcopy(bundle["items"][1])]

    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_diagnostic_bundle_provenance_hash(host, bundle),
        suffix=f"rereview-{variant}",
    )
    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)

    failed = DonorAnalysisPlugin().run(resume, host)

    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "RESUME_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_g2_resume_claim_legal_control_preserves_exact_atom_binding() -> None:
    host = Host()
    request = claim_request(host)
    plugin = DonorAnalysisPlugin()
    original = plugin.run(request, host)
    assert original is not None and original["contract_id"] == "candidate-batch/v1"
    original_key = host.stage_calls[-1]["operation_key"]
    resume = _resume_request(request, plugin, host)
    model_before = _g2_call_count(host, "host.model.invoke/v1")

    resumed = DonorAnalysisPlugin().run(resume, host)

    assert resumed == original
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == 2
    assert host.stage_calls[-1]["operation_key"] == original_key
    assert len(host.logical_stage_results) == 1
    assert host.completion_calls[-1]["outcome"] == "succeeded"


@pytest.mark.parametrize(
    "variant",
    [
        "claim-ordered-atom-ids",
        "claim-evidence",
        "accepted-evidence",
        "accepted-not-current",
        "accepted-not-accepted",
    ],
)
def test_g2_resume_claim_rejects_rebound_atom_binding_drift(variant: str) -> None:
    host = Host()
    request = claim_request(host)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(host, request, plugin)
    alternate_span = build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id="node-1",
        start_codepoint=4,
        end_codepoint=5,
        canonical_text=TEXT,
    )

    if variant.startswith("claim-"):
        item = bundle["items"][0]
        payload = json.loads(host.assets[item["payload_asset_id"]].decode("utf-8"))
        if variant == "claim-ordered-atom-ids":
            payload["ordered_atom_ids"] = ["atom-rebound"]
        else:
            payload["evidence_spans"] = [alternate_span]
        payload_id, payload_hash = host.seed_json(
            f"asset-g2-rebound-claim-{variant}", payload
        )
        item["payload_asset_id"] = payload_id
        item["mutation"]["payload_hash"] = payload_hash
        run_request = {
            key: deepcopy(value)
            for key, value in resume.items()
            if not key.startswith("resume_")
        }
        run_request["operation"] = "run"
        request_hash = hash_json(
            request["capability_id"] + "-request/v1", run_request
        )
        item_id = _g2_derived_id("candidate", request_hash, "0", payload_hash)
        entity_id = "book-claim:" + item_id.split(":", 1)[1]
        item["item_id"] = item_id
        item["target"]["entity_id"] = entity_id
        item["write_set"][0]["entity_id"] = entity_id
    elif variant == "accepted-evidence":
        resume["accepted_atoms"][0]["evidence_spans"] = [alternate_span]
    elif variant == "accepted-not-current":
        resume["accepted_atoms"][0]["is_current"] = False
    else:
        resume["accepted_atoms"][0]["status"] = "superseded"

    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_claim_bundle_provenance_hash(host, bundle),
        suffix=variant,
    )
    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)
    logical_stage_before = len(host.logical_stage_results)

    failed = DonorAnalysisPlugin().run(resume, host)

    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "RESUME_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before
    assert len(host.logical_stage_results) == logical_stage_before
    assert host.completion_calls[-1]["outcome"] == "failed"


def _g2_complete_bundle_provenance_hash(
    host: Host,
    request: dict,
    bundle: dict,
) -> str:
    """Recompute the complete v2 projection without trusting the original state."""
    payloads: list[dict] = []
    model_receipt_ids: list[str] = []
    for item in bundle["items"]:
        if item["schema"] == "candidate-item/v1":
            payload = json.loads(
                host.assets[item["payload_asset_id"]].decode("utf-8")
            )
            payloads.append(
                {
                    "asset_id": item["payload_asset_id"],
                    "content_hash": item["mutation"]["payload_hash"],
                    "payload": payload,
                }
            )
            if payload["schema"] == "book-atom/v1":
                receipt_id = payload["provenance"]["model"]["receipt_id"]
                if receipt_id not in model_receipt_ids:
                    model_receipt_ids.append(receipt_id)
            else:
                assert payload["schema"] == "book-claim/v1"
        else:
            assert item["schema"] == "diagnostic-item/v1"
            details = json.loads(
                host.assets[item["details_asset_id"]].decode("utf-8")
            )
            payloads.append(
                {
                    "asset_id": item["details_asset_id"],
                    "content_hash": item["details_hash"],
                    "details": details,
                }
            )
        for ref in item["source_refs"]:
            if ref["source_type"] == "model_receipt":
                receipt_id = ref["source_id"]
                if receipt_id not in model_receipt_ids:
                    model_receipt_ids.append(receipt_id)
    projection = {
        "schema": "donor-analysis-bundle-provenance/v2",
        "binding_hash": _g2_resume_binding_hash(request),
        "bundle": deepcopy(bundle),
        "payloads": payloads,
        "model_receipt_ids": model_receipt_ids,
        "skill_chain_result_refs": deepcopy(bundle["skill_chain_result_refs"]),
    }
    return hash_json("donor-analysis-bundle-provenance/v2", projection)


def _g2_execution_fence(host: Host) -> dict:
    return {
        "calls": len(host.calls),
        "model": _g2_call_count(host, "host.model.invoke/v1"),
        "stage": len(host.stage_calls),
        "logical_stage": len(host.logical_stage_results),
        "checkpoint": len(host.checkpoint_calls),
        "completion": len(host.completion_calls),
    }


def _g2_assert_resume_rejected_without_execution(
    host: Host,
    failed: dict | None,
    before: dict,
) -> None:
    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "RESUME_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == before["model"]
    assert len(host.stage_calls) == before["stage"]
    assert len(host.logical_stage_results) == before["logical_stage"]
    assert len(host.checkpoint_calls) == before["checkpoint"]
    assert len(host.completion_calls) == before["completion"] + 1
    assert host.completion_calls[-1]["outcome"] == "failed"
    forbidden = {"host.model.invoke/v1", "host.candidate.stage/v1"}
    assert not [
        method
        for method, _params in host.calls[before["calls"] :]
        if method in forbidden
    ]


def _g2_replace_exact_string(value, old: str, new: str) -> int:
    replacements = 0
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if child == old:
                value[key] = new
                replacements += 1
            else:
                replacements += _g2_replace_exact_string(child, old, new)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if child == old:
                value[index] = new
                replacements += 1
            else:
                replacements += _g2_replace_exact_string(child, old, new)
    return replacements


def _g2_substitute_all_model_receipt_carriers(
    host: Host,
    resume: dict,
    checkpoint: dict,
    state: dict,
    bundle: dict,
    *,
    replacement: str,
    suffix: str,
) -> int:
    original = "model-receipt-1"
    replacements = 0
    request_hash = hash_json(
        resume["capability_id"] + "-request/v1",
        {
            **{
                key: deepcopy(value)
                for key, value in resume.items()
                if not key.startswith("resume_")
            },
            "operation": "run",
        },
    )
    for ordinal, item in enumerate(bundle["items"]):
        if item["schema"] == "candidate-item/v1":
            payload = json.loads(
                host.assets[item["payload_asset_id"]].decode("utf-8")
            )
            payload_replacements = _g2_replace_exact_string(
                payload, original, replacement
            )
            replacements += payload_replacements
            if payload_replacements:
                payload_id, payload_hash = host.seed_json(
                    f"asset-g2-model-receipt-payload-{suffix}-{ordinal}", payload
                )
                item["payload_asset_id"] = payload_id
                item["mutation"]["payload_hash"] = payload_hash
                item_id = _g2_derived_id(
                    "candidate", request_hash, str(ordinal), payload_hash
                )
                old_item_id = item["item_id"]
                item["item_id"] = item_id
                old_entity_id = (
                    "book-atom:" + old_item_id.split(":", 1)[1]
                    if payload["schema"] == "book-atom/v1"
                    else "book-claim:" + old_item_id.split(":", 1)[1]
                )
                new_entity_id = (
                    "book-atom:" + item_id.split(":", 1)[1]
                    if payload["schema"] == "book-atom/v1"
                    else "book-claim:" + item_id.split(":", 1)[1]
                )
                if item["target"]["entity_id"] == old_entity_id:
                    item["target"]["entity_id"] = new_entity_id
                for write in item["write_set"]:
                    if write["entity_id"] == old_entity_id:
                        write["entity_id"] = new_entity_id
        else:
            details = json.loads(
                host.assets[item["details_asset_id"]].decode("utf-8")
            )
            detail_replacements = _g2_replace_exact_string(
                details, original, replacement
            )
            replacements += detail_replacements
            if detail_replacements:
                details_id, details_hash = host.seed_json(
                    f"asset-g2-model-receipt-details-{suffix}-{ordinal}", details
                )
                item["details_asset_id"] = details_id
                item["details_hash"] = details_hash

    replacements += _g2_replace_exact_string(bundle, original, replacement)
    replacements += _g2_replace_exact_string(state, original, replacement)
    replacements += _g2_replace_exact_string(checkpoint, original, replacement)
    return replacements


def _g2_request_for_resume_capability(host: Host, capability: str) -> dict:
    if capability == "atom":
        return atom_extract_request()
    if capability == "claim":
        return claim_request(host)
    assert capability == "rereview"
    return rereview_request(host)


@pytest.mark.parametrize("capability", ["atom", "claim", "rereview"])
def test_g2_f001_resume_rejects_synchronized_model_receipt_substitution(
    capability: str,
) -> None:
    host = Host()
    request = _g2_request_for_resume_capability(host, capability)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(
        host, request, plugin
    )

    replacements = _g2_substitute_all_model_receipt_carriers(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        replacement="model-receipt-unexecuted",
        suffix=capability,
    )
    assert replacements >= 1
    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_complete_bundle_provenance_hash(host, resume, bundle),
        suffix=f"model-receipt-{capability}",
    )
    before = _g2_execution_fence(host)

    failed = DonorAnalysisPlugin().run(resume, host)

    _g2_assert_resume_rejected_without_execution(host, failed, before)


@pytest.mark.parametrize("capability", ["atom", "claim", "rereview"])
def test_g2_f001_resume_rejects_structurally_valid_unexecuted_skill_chain_ref(
    capability: str,
) -> None:
    host = Host()
    request = _g2_request_for_resume_capability(host, capability)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(
        host, request, plugin
    )
    fake_ref = {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": "chain-unexecuted",
        "asset_id": None,
        "asset_hash": None,
        "result_bundle_id": bundle["bundle_id"],
        "result_item_id": bundle["items"][0]["item_id"],
        "stream_id": None,
        "acked_prefix_hash": None,
    }
    bundle["skill_chain_result_refs"] = [deepcopy(fake_ref)]
    state["skill_chain_result_refs"] = [deepcopy(fake_ref)]
    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_complete_bundle_provenance_hash(host, resume, bundle),
        suffix=f"unexecuted-skill-{capability}",
    )
    before = _g2_execution_fence(host)

    failed = DonorAnalysisPlugin().run(resume, host)

    _g2_assert_resume_rejected_without_execution(host, failed, before)


@pytest.mark.parametrize(
    "variant",
    [
        "bundle-id",
        "item-id",
        "item-kind",
        "target",
        "mutation-mode",
        "base",
        "write-set",
        "parents",
        "status-partial",
        "warnings",
    ],
)
def test_g2_f002_resume_rejects_synchronized_candidate_bundle_authority_mutation(
    variant: str,
) -> None:
    host = Host()
    if variant == "parents":
        second = deepcopy(
            host.model_outputs[
                "analysis.book.atom.extract-model-response/v1"
            ]["items"][0]
        )
        second["title"] = "第二个动作骤停"
        second["observation"] = "第二个动作也突然停止"
        host.model_outputs[
            "analysis.book.atom.extract-model-response/v1"
        ]["items"].append(second)
    request = atom_extract_request()
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(
        host, request, plugin
    )
    item = bundle["items"][0]

    if variant == "bundle-id":
        bundle["bundle_id"] = _g2_derived_id("bundle", "forged")
    elif variant == "item-id":
        item["item_id"] = _g2_derived_id("candidate", "forged")
    elif variant in {"item-kind", "mutation-mode"}:
        item["item_kind"] = "document"
        item["target"]["entity_kind"] = "document"
        item["write_set"][0]["entity_kind"] = "document"
        item["mutation"]["mode"] = (
            "replace" if variant == "item-kind" else "append_text"
        )
    elif variant == "target":
        item["target"]["entity_id"] = "document-victim"
        item["write_set"][0]["entity_id"] = "document-victim"
    elif variant == "base":
        item["base"]["revision_id"] = "rev-forged"
        item["write_set"][0]["revision_id"] = "rev-forged"
    elif variant == "write-set":
        item["write_set"].append(
            {
                "workspace_id": "ws-1",
                "entity_kind": "relation_set",
                "entity_id": "book-atom:additional-write",
                "revision_id": "rev-1",
                "content_hash": TEXT_HASH,
            }
        )
    elif variant == "parents":
        assert len(bundle["items"]) == 2
        bundle["items"][1]["parent_candidate_ids"] = [item["item_id"]]
    elif variant == "status-partial":
        item["status"] = "partial"
        bundle["partial"] = True
    else:
        bundle["warnings"] = [
            {
                "code": "forged_warning",
                "message": "structurally valid warning not emitted by the run",
                "details_asset_id": None,
            }
        ]

    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_complete_bundle_provenance_hash(host, resume, bundle),
        suffix=f"candidate-authority-{variant}",
    )
    before = _g2_execution_fence(host)

    failed = DonorAnalysisPlugin().run(resume, host)

    _g2_assert_resume_rejected_without_execution(host, failed, before)


@pytest.mark.parametrize(
    "variant",
    ["severity", "code", "message", "details", "item-id", "order"],
)
def test_g2_f002_resume_rejects_synchronized_rereview_diagnostic_mutation(
    variant: str,
) -> None:
    host = Host()
    request = _two_record_rereview(host)
    plugin = DonorAnalysisPlugin()
    assert plugin.run(request, host) is not None
    resume, checkpoint, state, bundle = _g2_ready_resume(
        host, request, plugin
    )
    item = bundle["items"][0]

    if variant == "severity":
        item["severity"] = "error"
    elif variant == "code":
        item["code"] = "forged_diagnostic"
    elif variant == "message":
        item["message"] = "forged diagnostic content"
    elif variant == "details":
        details = json.loads(
            host.assets[item["details_asset_id"]].decode("utf-8")
        )
        details["recommendation"] = "forged_but_structurally_closed"
        details_id, details_hash = host.seed_json(
            "asset-g2-forged-rereview-details", details
        )
        item["details_asset_id"] = details_id
        item["details_hash"] = details_hash
    elif variant == "item-id":
        item["item_id"] = _g2_derived_id("diagnostic", "forged")
    else:
        bundle["items"].reverse()

    _g2_rebind_result_bundle(
        host,
        resume,
        checkpoint,
        state,
        bundle,
        provenance_hash=_g2_complete_bundle_provenance_hash(host, resume, bundle),
        suffix=f"rereview-authority-{variant}",
    )
    before = _g2_execution_fence(host)

    failed = DonorAnalysisPlugin().run(resume, host)

    _g2_assert_resume_rejected_without_execution(host, failed, before)


def _g2_mixed_atom_claim_rereview(host: Host) -> dict:
    request = rereview_request(host)
    accepted_atoms: list[dict] = []
    ordered_atoms: list[dict] = []
    for ordinal, atom_id in enumerate(
        ["atom-rereview-accepted-1", "atom-rereview-accepted-2"]
    ):
        payload = atom_payload()
        payload["title"] = f"复审动作 {ordinal + 1}"
        payload["observation"] = f"复审动作证据 {ordinal + 1}"
        payload["provenance"] = trusted_atom_provenance()
        payload_asset_id, payload_hash = host.seed_json(
            f"asset-rereview-accepted-atom-{ordinal + 1}", payload
        )
        acceptance_ordinal = 21 + ordinal
        accepted_atoms.append(
            {
                "atom_id": atom_id,
                "source_revision_id": "rev-1",
                "status": "accepted",
                "is_current": True,
                "acceptance_ordinal": acceptance_ordinal,
                "payload_asset_id": payload_asset_id,
                "payload_hash": payload_hash,
                "evidence_spans": [evidence()],
            }
        )
        ordered_atoms.append(
            {
                "ordinal": ordinal,
                "atom_id": atom_id,
                "payload_hash": payload_hash,
                "acceptance_ordinal": acceptance_ordinal,
                "evidence_spans": [evidence()],
            }
        )

    claim_input_value = {
        "schema": "claim-input/v1",
        "source_revision_id": "rev-1",
        "ordered_atoms": ordered_atoms,
    }
    parameters_asset_id, parameters_hash = host.seed_json(
        "asset-rereview-claim-input", claim_input_value
    )
    claim = claim_payload(claim_input_value)
    claim_asset_id, claim_hash = host.seed_json(
        "asset-rereview-claim-candidate", claim
    )
    claim_authority = {
        "parameters_asset_id": parameters_asset_id,
        "parameters_asset_hash": parameters_hash,
        "snapshot_parameters_asset_id": parameters_asset_id,
        "snapshot_asset_hashes": [
            {"asset_id": parameters_asset_id, "sha256": parameters_hash}
        ],
        "accepted_atoms": accepted_atoms,
    }
    request["known_parent_candidate_ids"].append("candidate-parent-claim")
    request["candidate_records"].append(
        {
            "candidate_id": "candidate-successor-claim",
            "candidate_kind": "book_claim",
            "status": "pending",
            "is_current": False,
            "payload_hash": claim_hash,
            "payload_asset_id": claim_asset_id,
            "source_revision_id": "rev-1",
            "parent_candidate_id": "candidate-parent-claim",
            "successor_candidate_id": "candidate-successor-claim",
            "claim_authority": claim_authority,
        }
    )
    host.model_outputs["analysis.book.rereview-model-response/v1"]["items"].append(
        {
            "candidate_id": "candidate-successor-claim",
            "severity": "info",
            "code": "claim_authority_ok",
            "message": "Claim authority is exact",
            "recommendation": "retain",
            "successor_candidate_id": "candidate-successor-claim",
        }
    )
    return request


def _g2_alternate_evidence() -> dict:
    return build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id="node-1",
        start_codepoint=4,
        end_codepoint=5,
        canonical_text=TEXT,
    )


def _g2_mutate_rereview_claim_authority(
    host: Host,
    request: dict,
    variant: str,
) -> None:
    record = request["candidate_records"][1]
    payload = json.loads(host.assets[record["payload_asset_id"]].decode("utf-8"))
    payload_changed = True
    if variant == "claim-evidence-drift":
        payload["evidence_spans"][0] = _g2_alternate_evidence()
    elif variant == "claim-evidence-empty":
        payload["evidence_spans"] = []
    elif variant == "unknown-atom-id":
        payload["ordered_atom_ids"][1] = "atom-unknown"
    elif variant == "reordered-atom-ids":
        payload["ordered_atom_ids"].reverse()
    elif variant == "empty-title":
        payload["title"] = ""
    elif variant == "invalid-confidence":
        payload["interpretation_confidence"] = "forged"
    elif variant == "invalid-method":
        payload["analysis_method"] = {
            "name": "book-claim-generate",
            "version": "2",
        }
    elif variant == "invalid-counterexamples":
        payload["counterexamples"] = [{"not": "a string"}]
    elif variant == "invalid-conflicts":
        payload["conflicts"] = [1]
    elif variant == "prompt-eligible":
        payload["prompt_eligible"] = True
    elif variant == "promotion-authorized":
        payload["promotion_authorized"] = True
    else:
        payload_changed = False
        authority = record["claim_authority"]
        accepted = authority["accepted_atoms"][1]
        if variant == "accepted-evidence-drift":
            accepted["evidence_spans"] = [_g2_alternate_evidence()]
        elif variant == "accepted-evidence-empty":
            accepted["evidence_spans"] = []
        elif variant == "payload-hash-drift":
            first = authority["accepted_atoms"][0]
            accepted["payload_asset_id"] = first["payload_asset_id"]
            accepted["payload_hash"] = first["payload_hash"]
        elif variant == "acceptance-ordinal-drift":
            accepted["acceptance_ordinal"] = 99
        elif variant == "current-drift":
            accepted["is_current"] = False
        elif variant == "status-drift":
            accepted["status"] = "superseded"
        else:
            assert variant == "revision-drift"
            accepted["source_revision_id"] = "rev-other"

    if payload_changed:
        payload_asset_id, payload_hash = host.seed_json(
            f"asset-g2-invalid-rereview-claim-{variant}", payload
        )
        record["payload_asset_id"] = payload_asset_id
        record["payload_hash"] = payload_hash


@pytest.mark.parametrize(
    "variant",
    [
        "claim-evidence-drift",
        "claim-evidence-empty",
        "accepted-evidence-drift",
        "accepted-evidence-empty",
        "unknown-atom-id",
        "reordered-atom-ids",
        "payload-hash-drift",
        "acceptance-ordinal-drift",
        "current-drift",
        "status-drift",
        "revision-drift",
        "empty-title",
        "invalid-confidence",
        "invalid-method",
        "invalid-counterexamples",
        "invalid-conflicts",
        "prompt-eligible",
        "promotion-authorized",
    ],
)
def test_g2_f003_rereview_rejects_invalid_claim_authority_before_model(
    variant: str,
) -> None:
    host = Host()
    request = _g2_mixed_atom_claim_rereview(host)
    _g2_mutate_rereview_claim_authority(host, request, variant)
    before = _g2_execution_fence(host)

    failed = DonorAnalysisPlugin().run(request, host)

    assert failed is not None
    assert _g2_failure_details(host, failed)["code"] == "REREVIEW_INVALID"
    assert _g2_call_count(host, "host.model.invoke/v1") == 0
    assert host.stage_calls == []
    assert host.logical_stage_results == {}
    assert host.checkpoint_calls == []
    assert len(host.completion_calls) == before["completion"] + 1
    assert host.completion_calls[-1]["outcome"] == "failed"
    assert not [
        method
        for method, _params in host.calls
        if method in {"host.model.invoke/v1", "host.candidate.stage/v1"}
    ]


def test_g2_f003_legal_ordered_mixed_atom_claim_rereview_run_and_resume_is_diagnostic_only() -> None:
    host = Host()
    request = _g2_mixed_atom_claim_rereview(host)
    plugin = DonorAnalysisPlugin()

    original = plugin.run(request, host)

    assert original is not None and original["contract_id"] == "diagnostic-bundle/v1"
    assert original["partial"] is False
    expected_candidate_ids = [
        record["candidate_id"] for record in request["candidate_records"]
    ]
    details = [
        json.loads(host.assets[item["details_asset_id"]].decode("utf-8"))
        for item in original["items"]
    ]
    assert [item["candidate_id"] for item in details] == expected_candidate_ids
    assert [item["lineage"] for item in details] == [
        {
            "predecessor_candidate_id": record["parent_candidate_id"],
            "successor_candidate_id": record["successor_candidate_id"],
        }
        for record in request["candidate_records"]
    ]
    assert all(item["mutation_staged"] is False for item in details)
    assert _g2_call_count(host, "host.model.invoke/v1") == 1
    assert host.stage_calls == []
    assert host.logical_stage_results == {}

    resume = _resume_request(request, plugin, host)
    model_before = _g2_call_count(host, "host.model.invoke/v1")
    stage_before = len(host.stage_calls)
    logical_stage_before = len(host.logical_stage_results)
    resumed = DonorAnalysisPlugin().run(resume, host)

    assert resumed == original
    assert _g2_call_count(host, "host.model.invoke/v1") == model_before
    assert len(host.stage_calls) == stage_before == 0
    assert len(host.logical_stage_results) == logical_stage_before == 0
    assert host.completion_calls[-1]["outcome"] == "succeeded"
