from __future__ import annotations

import json
import threading
from copy import deepcopy

from plotpilot_plugin_sdk.verifier import (
    verify_checkpoint,
    verify_provenance_receipt,
    verify_result_bundle,
)
from style_manufacturing.contract import style_payload_hash, style_release_id
from style_runtime.runtime import (
    CAPABILITY_APPLY,
    CAPABILITY_REFINE,
    CAPABILITY_REVIEW,
    StyleRuntimePlugin,
)
from test_contract_semantics import qualification, style_pack
from test_runtime_manufacturing import StyleHost


class RuntimeHost(StyleHost):
    def __init__(self) -> None:
        super().__init__()
        self.model_outputs.update(
            {
                "style.apply-model-response/v1": {
                    "schema": "style.apply-model-response/v1",
                    "items": [{"text": "雨落在旧城，脚步停在门前。"}],
                },
                "style.review-model-response/v1": {
                    "schema": "style.review-model-response/v1",
                    "items": [
                        {
                            "severity": "warning",
                            "code": "STYLE_RHYTHM_DRIFT",
                            "message": "句式节奏偏离目标",
                            "dimension": "rhythm",
                            "score_0_100": 72,
                            "recommendation": "增加短长句交替",
                        }
                    ],
                },
                "style.refine-model-response/v1": {
                    "schema": "style.refine-model-response/v1",
                    "items": [{"text": "雨落旧城。脚步一顿，停在门前。"}],
                },
            }
        )


def runtime_common(capability: str, host: RuntimeHost, *, operation: str = "run") -> dict:
    source = "雨落在旧城，脚步停在门前。".encode()
    source_id, source_hash = host.seed_bytes("asset-runtime-source", source)
    pack = style_pack()
    pack["target_cas"] = {
        "workspace_id": "workspace:runtime",
        "entity_id": "document:chapter-1",
        "base_revision_id": "revision:chapter-1",
        "base_content_hash": source_hash,
    }
    pack["lexicon_bindings"] = []
    pack["payload_hash"] = style_payload_hash(pack)
    pack["style_release_id"] = style_release_id(
        pack["style_pack_id"], pack["version"], pack["payload_hash"]
    )
    receipt = qualification(pack)
    pack_id, pack_hash = host.seed_json("asset-runtime-style", pack)
    receipt_id, receipt_hash = host.seed_json("asset-runtime-qualification", receipt)
    schemas = {
        CAPABILITY_APPLY: "style.apply-request/v1",
        CAPABILITY_REVIEW: "style.review-request/v1",
        CAPABILITY_REFINE: "style.refine-request/v1",
    }
    return {
        "schema": schemas[capability],
        "capability_id": capability,
        "operation_key": capability,
        "operation": operation,
        "job_id": "job-runtime-1",
        "step_id": "step-runtime-1",
        "attempt_id": "attempt-runtime-1",
        "worker_run_id": "worker-runtime-1",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-runtime-1"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "b" * 64,
        "workspace_id": "workspace:runtime",
        "document_id": "document:chapter-1",
        "source_revision_id": "revision:chapter-1",
        "source_asset_id": source_id,
        "source_content_hash": source_hash,
        "style_pack_asset_id": pack_id,
        "style_pack_asset_hash": pack_hash,
        "qualification_receipt_asset_id": receipt_id,
        "qualification_receipt_asset_hash": receipt_hash,
        "model_profile_revision_id": "model-profile:runtime",
    }


def apply_request(host: RuntimeHost, *, operation: str = "run") -> dict:
    return {
        **runtime_common(CAPABILITY_APPLY, host, operation=operation),
        "lexicon_assets": [],
        "candidate_count": 1,
    }


def review_request(host: RuntimeHost, *, operation: str = "run") -> dict:
    rubric_id, rubric_hash = host.seed_json("asset-runtime-rubric", {"schema": "quality-rubric/v1"})
    return {
        **runtime_common(CAPABILITY_REVIEW, host, operation=operation),
        "quality_rubric_asset_id": rubric_id,
        "quality_rubric_asset_hash": rubric_hash,
    }


def refine_request(host: RuntimeHost, review_bundle: dict, *, operation: str = "run") -> dict:
    rubric_id, rubric_hash = host.seed_json("asset-refine-rubric", {"schema": "quality-rubric/v1"})
    review_id, review_hash = host.seed_json("asset-review-result", review_bundle)
    return {
        **runtime_common(CAPABILITY_REFINE, host, operation=operation),
        "quality_rubric_asset_id": rubric_id,
        "quality_rubric_asset_hash": rubric_hash,
        "review_result_asset_id": review_id,
        "review_result_asset_hash": review_hash,
        "known_parent_candidate_ids": ["candidate:parent-1"],
        "candidate_count": 1,
    }


def resume_request(request: dict, checkpoint: dict) -> dict:
    return {
        **request,
        "operation": "resume",
        "resume_checkpoint_asset_id": checkpoint["checkpoint_asset_id"],
        "resume_checkpoint_asset_hash": checkpoint["checkpoint_asset_hash"],
        "resume_state_asset_id": checkpoint["state_asset_id"],
        "resume_state_asset_hash": checkpoint["state_asset_hash"],
    }


def test_apply_outputs_candidate_only_and_binds_exact_style_qualification_receipt() -> None:
    host = RuntimeHost()
    plugin = StyleRuntimePlugin()
    request = apply_request(host)
    assert plugin.validate(request, host)["valid"] is True
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    assert bundle["bundle_type"] == "candidate_batch" and bundle["partial"] is False
    item = bundle["items"][0]
    assert item["schema"] == "candidate-item/v1" and item["status"] == "complete"
    assert {ref["source_type"] for ref in item["source_refs"]} == {
        "canonical_revision", "style_release", "qualification_receipt", "model_receipt"
    }
    verify_result_bundle(
        bundle,
        snapshot_workspace_id=request["workspace_id"],
        snapshot_hash_value=request["run_snapshot_hash"],
    )
    assert plugin.last_checkpoint is not None and plugin.last_receipt is not None
    verify_checkpoint(plugin.last_checkpoint["checkpoint"], expected_snapshot_hash=request["run_snapshot_hash"])
    verify_provenance_receipt(plugin.last_receipt)
    assert host.stage_calls and host.completion_calls[-1]["outcome"] == "succeeded"


def test_review_is_diagnostic_only_and_refine_is_a_user_reviewable_successor_candidate() -> None:
    host = RuntimeHost()
    review_plugin = StyleRuntimePlugin()
    review = review_plugin.run(review_request(host), host)
    assert review is not None and review["contract_id"] == "diagnostic-bundle/v1"
    assert review["bundle_type"] == "diagnostic" and review["items"][0]["status"] == "complete"
    assert host.stage_calls == []

    refine_plugin = StyleRuntimePlugin()
    request = refine_request(host, review)
    refined = refine_plugin.run(request, host)
    assert refined is not None and refined["contract_id"] == "candidate-batch/v1"
    assert refined["items"][0]["parent_candidate_ids"] == ["candidate:parent-1"]
    assert len(host.stage_calls) == 1
    verify_result_bundle(
        refined,
        snapshot_workspace_id=request["workspace_id"],
        snapshot_hash_value=request["run_snapshot_hash"],
        known_parent_ids={"candidate:parent-1"},
    )


def test_runtime_resume_reuses_exact_result_without_model_replay_and_tamper_fails_closed() -> None:
    host = RuntimeHost()
    plugin = StyleRuntimePlugin()
    request = apply_request(host)
    first = plugin.run(request, host)
    assert first is not None and plugin.last_checkpoint is not None
    checkpoint = deepcopy(plugin.last_checkpoint)
    before = sum(method == "host.model.invoke/v1" for method, _ in host.calls)
    resumed = plugin.run(resume_request(request, checkpoint), host)
    assert resumed == first
    assert sum(method == "host.model.invoke/v1" for method, _ in host.calls) == before
    assert host.stage_calls[0]["operation_key"] == host.stage_calls[1]["operation_key"]

    state = json.loads(host.assets[checkpoint["state_asset_id"]].decode("utf-8"))
    state["style_payload_hash"] = "0" * 64
    state_id, state_hash = host.seed_json("asset-runtime-state-tamper", state)
    tampered = resume_request(request, checkpoint)
    tampered["resume_state_asset_id"] = state_id
    tampered["resume_state_asset_hash"] = state_hash
    failure = plugin.run(tampered, host)
    assert failure is not None and failure["contract_id"] == "diagnostic-bundle/v1"
    assert failure["partial"] is True and failure["items"][0]["code"] == "RESUME_INVALID"


def test_runtime_cancel_after_checkpoint_never_stages_or_returns_candidate() -> None:
    host = RuntimeHost()
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = StyleRuntimePlugin()
    request = apply_request(host)
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(plugin.run(request, host)))
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancel = {**request, "operation": "cancel"}
    assert plugin.run(cancel, host) == {"accepted": True, "worker_run_id": request["worker_run_id"]}
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and result == [None]
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "cancelled"


def test_unqualified_or_stale_style_is_a_legal_failure_result_before_model_or_stage() -> None:
    host = RuntimeHost()
    request = apply_request(host)
    receipt = json.loads(host.assets[request["qualification_receipt_asset_id"]].decode("utf-8"))
    receipt["style_release_id"] = "0" * 64
    bad_id, bad_hash = host.seed_json("asset-stale-qualification", receipt)
    request["qualification_receipt_asset_id"] = bad_id
    request["qualification_receipt_asset_hash"] = bad_hash
    plugin = StyleRuntimePlugin()
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["partial"] is True and bundle["items"][0]["code"] == "STYLE_NOT_ELIGIBLE"
    assert not any(method == "host.model.invoke/v1" for method, _ in host.calls)
    assert host.stage_calls == [] and host.completion_calls[-1]["outcome"] == "failed"
    verify_result_bundle(
        bundle,
        snapshot_hash_value=request["run_snapshot_hash"],
        attempt_state="failed",
    )
    assert plugin.last_receipt is not None
    verify_provenance_receipt(plugin.last_receipt)
