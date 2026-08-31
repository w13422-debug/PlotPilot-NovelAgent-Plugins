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
for path in (
    ROOT / "sdk",
    ROOT / "plugins" / "narrative-analysis",
    ROOT / "plugins" / "outline-projection",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_attempt_result,
    verify_checkpoint,
    verify_provenance_receipt,
    verify_result_bundle,
)
from narrative_analysis.contract import build_evidence_span  # noqa: E402
from narrative_analysis.runtime import (  # noqa: E402
    CAPABILITY_PLAN_COMPILE,
    CAPABILITY_SYNTHESIZE,
    CAPABILITY_UNIT_EXTRACT,
    NarrativeAnalysisPlugin,
)
from outline_projection.runtime import (  # noqa: E402
    OutlineProjectionPlugin,
    TerminalContractError as OutlineTerminalContractError,
    main as outline_main,
)


TEXT = "甲🙂乙转折丙"
TEXT_BYTES = TEXT.encode("utf-8")
TEXT_HASH = hashlib.sha256(TEXT_BYTES).hexdigest()
NODES = [{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": len(TEXT)}]
SNAPSHOT_HASH = "1" * 64
DATA_ROOT = ROOT / "data" / "plot-structure" / "v1"
DATA_TEMPLATE = json.loads((DATA_ROOT / "data" / "templates.json").read_text(encoding="utf-8"))
DATA_IDENTITY = json.loads((DATA_ROOT / "identity.json").read_text(encoding="utf-8"))
TEMPLATE_BINDING = {
    "data_plugin_id": DATA_IDENTITY["plugin_id"],
    "data_release_id": DATA_IDENTITY["release_id"],
    "package_hash": DATA_IDENTITY["package_hash"],
    "format_id": "plot-structure-template/v1",
    "template_id": "general-longform-seven-beat",
}


def json_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def valid_unit(*, child_receipt_id: str | None = None) -> dict:
    span = {
        "evidence_span_id": "span-1",
        **build_evidence_span(
            workspace_id="ws-1",
            document_id="doc-1",
            revision_id="rev-1",
            node_id="node-1",
            start_codepoint=1,
            end_codepoint=5,
            canonical_text=TEXT,
        ),
    }
    return {
        "schema": "narrative-unit/v1",
        "unit_id": "unit-1",
        "unit_kind": "turn",
        "title": "转折",
        "summary": "证据绑定剧情转折",
        "order": 0,
        "evidence_spans": [span],
        "source_attributions": [{
            "attribution_id": "attribution-1",
            "source_type": "canonical_revision",
            "workspace_id": "ws-1",
            "source_id": "doc-1",
            "revision_or_hash": "rev-1",
            "evidence_span_ids": ["span-1"],
            "child_receipt_id": child_receipt_id,
        }],
        "relations": [],
        "provenance": {"source_mode": "model", "model_receipt_id": "placeholder"},
    }


class Host:
    """Public Host RPC fake with durable Asset, stage and checkpoint replay."""

    def __init__(self) -> None:
        taxonomy = {"schema": "book-analysis-taxonomy/v1", "format_id": "book-analysis-taxonomy/v1"}
        # Use the exact immutable Data package bytes.  The remediation runtime
        # deliberately rejects a hand-written/partial template object.
        template = DATA_TEMPLATE
        self.assets: dict[str, bytes] = {
            "asset-canonical": TEXT_BYTES,
            "asset-taxonomy": canonical_bytes(taxonomy),
            "asset-template": canonical_bytes(template),
            "asset-child-input": canonical_bytes({"schema": "broker-input/v1", "binding_id": "binding-1"}),
            "asset-child-params": canonical_bytes({"schema": "broker-parameters/v1", "binding_id": "binding-1"}),
        }
        self.uploads: dict[str, bytearray] = {}
        self.upload_results: dict[tuple[str, int], tuple[str, dict]] = {}
        self.logical_stage_results: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.stage_calls: list[dict] = []
        self.completion_calls: list[dict] = []
        self.checkpoint_calls: list[dict] = []
        self.block_checkpoint = False
        self.checkpoint_entered = threading.Event()
        self.checkpoint_release = threading.Event()
        self.checkpoint_release.set()
        self._event_seq = 0
        self.child_job_id = "child-job-1"
        self.child_cancel_calls: list[dict] = []
        self.model_requests: list[dict] = []
        self.tamper_model_request: str | None = None

    def seed_json(self, asset_id: str, value: object) -> tuple[str, str]:
        raw = canonical_bytes(value)
        self.assets[asset_id] = raw
        return asset_id, hashlib.sha256(raw).hexdigest()

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def call(self, method: str, params: dict) -> dict:
        params = dict(params)
        self.calls.append((method, params))
        if method == "host.asset.read/v1":
            data = self.assets[params["asset_id"]]
            offset = params["offset"]
            page = data[offset : offset + params["length"]]
            return {
                "base64_chunk": base64.b64encode(page).decode("ascii"),
                "next_offset": None if offset + len(page) >= len(data) else offset + len(page),
                "content_hash": hashlib.sha256(page).hexdigest(),
            }
        if method == "host.asset.create/v1":
            replay_key = (params["upload_id"], params["offset"])
            request_hash = hashlib.sha256(canonical_bytes(params)).hexdigest()
            if replay_key in self.upload_results:
                previous_hash, previous = self.upload_results[replay_key]
                assert previous_hash == request_hash
                return deepcopy(previous)
            chunk = base64.b64decode(params["base64_chunk"], validate=True)
            assert hashlib.sha256(chunk).hexdigest() == params["chunk_hash"]
            buffer = self.uploads.setdefault(params["upload_id"], bytearray())
            assert params["offset"] == len(buffer)
            buffer.extend(chunk)
            complete = bool(params["final"])
            asset_id = None
            if complete:
                raw = bytes(buffer)
                assert len(raw) == params["total_size"]
                assert hashlib.sha256(raw).hexdigest() == params["expected_hash"]
                asset_id = "asset-" + hashlib.sha256(raw).hexdigest()[:40]
                self.assets[asset_id] = raw
                # Adversarial Host fixture: mutate the persisted model-request
                # Asset after upload.  The worker must re-read and hash the
                # exact bytes before invoking a provider, so deletion and
                # replacement of canonical_text fail closed.
                if (self.tamper_model_request in {"delete", "replace"}
                        and str(params["upload_id"]).endswith("-upload-model-request")):
                    tampered = json.loads(raw.decode("utf-8"))
                    if self.tamper_model_request == "delete":
                        tampered.pop("canonical_text", None)
                    else:
                        tampered["canonical_text"] = "替换正文"
                    self.assets[asset_id] = canonical_bytes(tampered)
            result = {
                "upload_id": params["upload_id"],
                "accepted_bytes": len(buffer),
                "completed": complete,
                "asset_id": asset_id,
            }
            self.upload_results[replay_key] = (request_hash, deepcopy(result))
            return result
        if method == "host.asset.upload.status/v1":
            raw = bytes(self.uploads[params["upload_id"]])
            assert hashlib.sha256(raw).hexdigest() == params["expected_hash"]
            return {
                "accepted_bytes": len(raw),
                "completed": True,
                "asset_id": "asset-" + hashlib.sha256(raw).hexdigest()[:40],
            }
        if method == "host.model.invoke/v1":
            model_request = json.loads(self.assets[params["request_asset_id"]].decode("utf-8"))
            self.model_requests.append(deepcopy(model_request))
            if self.tamper_model_request == "delete":
                model_request.pop("canonical_text", None)
            elif self.tamper_model_request == "replace":
                model_request["canonical_text"] = "替换正文"
            output_schema = model_request["output_schema"]
            if output_schema == "analysis.narrative.unit.extract-model-response/v1":
                unit = valid_unit()
                unit.pop("provenance")
                output = {"schema": output_schema, "items": [unit]}
            elif output_schema == "analysis.narrative.synthesize-model-response/v1":
                output = {
                    "schema": output_schema,
                    "synthesis": {
                        "synthesis_id": "synthesis-runtime-1",
                        "title": "运行时综合",
                        "summary": "保留 Broker 与证据闭包",
                        "sections": [{
                            "section_id": "section-runtime-1",
                            "title": "第一节",
                            "summary": "转折",
                            "unit_ids": ["unit-1"],
                            "evidence_span_ids": ["span-1"],
                            "source_attribution_ids": ["attribution-1"],
                        }],
                    },
                }
            else:
                raise AssertionError(output_schema)
            response_id, _ = self.seed_json(
                "asset-model-response-" + hashlib.sha256(output_schema.encode()).hexdigest()[:12],
                output,
            )
            return {
                "state": "received",
                "response_asset_id": response_id,
                "receipt_id": "model-receipt-1",
                "uncertainty": None,
            }
        if method == "host.capability.invoke/v1":
            # The public RPC result is intentionally complete: the narrative
            # worker records it durably and then polls the same child.
            return {
                "accepted": True,
                "child_job_id": self.child_job_id,
                "child_step_id": "child-step-1",
                "child_run_snapshot_asset_id": "asset-child-snapshot-1",
                "child_run_snapshot_hash": "4" * 64,
                "child_result_contract": "candidate-batch/v1",
                "child_job_event_seq": self._next_seq(),
            }
        if method == "host.capability.poll/v1":
            return {
                "job_snapshot_asset_id": "asset-child-job-snapshot",
                "job_event_page_asset_id": "asset-child-event-page",
                "next_job_event_seq": 1,
                "terminal": True,
                "result_bundle_asset_id": "asset-child-bundle-1",
                "provenance_receipt_id": "child-receipt-1",
            }
        if method == "host.capability.cancel/v1":
            self.child_cancel_calls.append(params)
            return {
                "accepted": True,
                "terminal_known": False,
                "child_state": "cancelling",
                "child_job_event_seq": self._next_seq(),
            }
        if method == "host.job.event/v1":
            return {"accepted": True, "job_event_seq": self._next_seq()}
        if method == "host.checkpoint.commit/v1":
            self.checkpoint_calls.append(params)
            if self.block_checkpoint:
                self.block_checkpoint = False
                self.checkpoint_entered.set()
                assert self.checkpoint_release.wait(timeout=5)
            checkpoint = json.loads(self.assets[params["checkpoint_asset_id"]].decode("utf-8"))
            return {
                "accepted": True,
                "checkpoint_id": checkpoint.get("checkpoint_id", "checkpoint-1"),
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self._next_seq(),
            }
        if method == "host.candidate.stage/v1":
            self.stage_calls.append(params)
            if params["operation_key"] in self.logical_stage_results:
                return deepcopy(self.logical_stage_results[params["operation_key"]])
            bundle = json.loads(self.assets[params["result_bundle_asset_id"]].decode("utf-8"))
            result = {
                "accepted": True,
                "staged_items": [{
                    "item_id": item["item_id"],
                    "candidate_id": ("candidate-" + item["item_id"])[:128],
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                } for item in bundle["items"]],
                "job_event_seq": self._next_seq(),
            }
            self.logical_stage_results[params["operation_key"]] = deepcopy(result)
            return result
        if method == "host.job.complete/v1":
            self.completion_calls.append(params)
            sequence = self._next_seq()
            return {
                "accepted": True,
                "attempt_state": params["outcome"],
                "step_state": params["outcome"],
                "job_state": params["outcome"],
                "provenance_receipt_id": "receipt-test",
                "job_event_seq": sequence,
                "core_event_high_water": sequence,
            }
        raise AssertionError(f"unexpected Host method: {method}")


def unit_request(*, operation: str = "run", worker_run_id: str = "worker-1") -> dict:
    return {
        "schema": "analysis.narrative.unit.extract-request/v1",
        "capability_id": CAPABILITY_UNIT_EXTRACT,
        "operation_key": CAPABILITY_UNIT_EXTRACT,
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
        "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "taxonomy_asset_id": "asset-taxonomy",
        "taxonomy_asset_hash": hashlib.sha256(canonical_bytes({"schema": "book-analysis-taxonomy/v1", "format_id": "book-analysis-taxonomy/v1"})).hexdigest(),
        "plot_template_asset_id": "asset-template",
        "plot_template_asset_hash": hashlib.sha256(canonical_bytes(DATA_TEMPLATE)).hexdigest(),
        "model_profile_revision_id": "model-profile-1",
    }


def test_narrative_run_stages_candidate_and_emits_checkpoint_receipt_source_closure() -> None:
    host = Host()
    plugin = NarrativeAnalysisPlugin()
    bundle = plugin.run(unit_request(), host)
    assert bundle is not None
    verify_result_bundle(bundle, snapshot_workspace_id="ws-1", snapshot_hash_value=SNAPSHOT_HASH)
    verify_checkpoint(plugin.last_checkpoint["checkpoint"], expected_snapshot_hash=SNAPSHOT_HASH)
    verify_provenance_receipt(plugin.last_receipt)
    payload = json.loads(host.assets[bundle["items"][0]["payload_asset_id"]].decode("utf-8"))
    assert payload["evidence_spans"][0]["quote"] == "🙂乙转折"
    assert payload["source_attributions"][0]["evidence_span_ids"] == ["span-1"]
    assert payload["provenance"] == {"source_mode": "model", "model_receipt_id": "model-receipt-1"}
    assert plugin.last_stage_response["staged_items"][0]["publication_eligibility"] == "review_only"
    assert host.completion_calls[-1]["outcome"] == "succeeded"


def test_narrative_resume_reuses_bound_checkpoint_and_durable_result() -> None:
    host = Host()
    plugin = NarrativeAnalysisPlugin()
    original = plugin.run(unit_request(), host)
    checkpoint = deepcopy(plugin.last_checkpoint)
    request = unit_request(operation="resume")
    request.update({
        "resume_checkpoint_asset_id": checkpoint["checkpoint_asset_id"],
        "resume_checkpoint_asset_hash": checkpoint["checkpoint_asset_hash"],
        "resume_state_asset_id": checkpoint["state_asset_id"],
        "resume_state_asset_hash": checkpoint["state_asset_hash"],
    })
    resumed = plugin.run(request, host)
    assert resumed == original
    assert len(host.stage_calls) == 2
    assert host.completion_calls[-1]["outcome"] == "succeeded"
    verify_provenance_receipt(plugin.last_receipt)


def test_narrative_cancel_during_checkpoint_prevents_candidate_stage() -> None:
    host = Host()
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = NarrativeAnalysisPlugin()
    outcome: dict[str, object] = {}

    def worker() -> None:
        outcome["bundle"] = plugin.run(unit_request(worker_run_id="worker-cancel"), host)

    thread = threading.Thread(target=worker)
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancel = plugin.run(unit_request(operation="cancel", worker_run_id="worker-cancel"), host)
    assert cancel == {"accepted": True, "worker_run_id": "worker-cancel"}
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["bundle"] is None
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "cancelled"


def test_narrative_failure_returns_diagnostic_bundle_without_staging() -> None:
    host = Host()
    plugin = NarrativeAnalysisPlugin()
    request = unit_request()
    request["canonical_text_hash"] = "f" * 64
    bundle = plugin.run(request, host)
    assert bundle is not None
    assert bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["items"][0]["status"] == "failed"
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"
    verify_provenance_receipt(plugin.last_receipt)


def seed_plan_and_unit(host: Host, *, child_receipt_id: str | None = None) -> tuple[dict, str, str, str, str]:
    unit = valid_unit(child_receipt_id=child_receipt_id)
    unit["provenance"] = {"source_mode": "broker" if child_receipt_id else "model", "model_receipt_id": None if child_receipt_id else "model-receipt-1"}
    unit_id, unit_hash = host.seed_json("asset-unit-1", unit)
    plan = {
        "schema": "narrative-plan/v1", "plan_id": "plan-1", "version": 1,
        "template_binding": deepcopy(TEMPLATE_BINDING),
        "unit_refs": [{"unit_id": "unit-1", "payload_asset_id": unit_id, "payload_hash": unit_hash,
                       "order": 0, "evidence_span_ids": ["span-1"],
                       "source_attribution_ids": ["attribution-1"]}],
        "hierarchy": [{"node_id": "book-1", "parent_node_id": None, "level": "book", "title": "全书",
                       "unit_ids": ["unit-1"], "order": 0}],
        "created_from_snapshot_hash": SNAPSHOT_HASH, "immutable": True,
    }
    plan_id, plan_hash = host.seed_json("asset-plan-1", plan)
    return plan, plan_id, plan_hash, unit_id, unit_hash


def test_plan_validate_compiles_business_candidate_without_staging_or_terminal_side_effects() -> None:
    host = Host()
    _, _, _, unit_id, unit_hash = seed_plan_and_unit(host)
    request = {
        "schema": "analysis.narrative.plan.compile-request/v1", "capability_id": CAPABILITY_PLAN_COMPILE,
        "operation_key": CAPABILITY_PLAN_COMPILE, "operation": "validate", "job_id": "job-plan-1",
        "step_id": "step-plan-1", "attempt_id": "attempt-plan-1", "worker_run_id": "worker-plan-1",
        "lease_epoch": 1, "checkpoint_ids": ["checkpoint-plan-1"], "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z", "total_units": 1, "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1", "document_id": "doc-1", "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical", "canonical_text_hash": TEXT_HASH, "nodes": deepcopy(NODES),
        "narrative_unit_assets": [{"asset_id": unit_id, "asset_hash": unit_hash}],
        "template_asset_id": "asset-template", "template_asset_hash": hashlib.sha256(host.assets["asset-template"]).hexdigest(),
        "template_binding": deepcopy(TEMPLATE_BINDING),
        "plan_id": "plan-validated-1", "plan_version": 1,
        "hierarchy": [{"node_id": "book-1", "parent_node_id": None, "level": "book", "title": "全书",
                       "unit_ids": ["unit-1"], "order": 0}],
    }
    plugin = NarrativeAnalysisPlugin()
    bundle = plugin.run(request, host)
    # The frozen seven-beat template is executable input.  A one-book-node
    # shortcut omits its required volume/chapter/plot-unit levels and must
    # fail closed rather than emit an immutable business Plan.
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["items"][0]["code"] == "NARRATIVE_PLAN_INVALID"
    verify_provenance_receipt(plugin.last_receipt)
    assert plugin.last_stage_response is None
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_plan_validate_compiles_template_interpreted_multi_level_candidate() -> None:
    host = Host()
    bindings: list[dict[str, str]] = []
    for index, level in enumerate(("book", "volume", "chapter", "plot_unit"), start=1):
        unit = valid_unit()
        unit["unit_id"] = f"unit-{index}"
        unit["order"] = index - 1
        unit["evidence_spans"][0]["evidence_span_id"] = f"span-{index}"
        unit["source_attributions"][0]["attribution_id"] = f"attribution-{index}"
        unit["source_attributions"][0]["evidence_span_ids"] = [f"span-{index}"]
        asset_id, asset_hash = host.seed_json(f"asset-unit-{index}", unit)
        bindings.append({"asset_id": asset_id, "asset_hash": asset_hash})
    request = {
        "schema": "analysis.narrative.plan.compile-request/v1", "capability_id": CAPABILITY_PLAN_COMPILE,
        "operation_key": CAPABILITY_PLAN_COMPILE, "operation": "validate", "job_id": "job-plan-valid",
        "step_id": "step-plan-valid", "attempt_id": "attempt-plan-valid", "worker_run_id": "worker-plan-valid",
        "lease_epoch": 1, "checkpoint_ids": ["checkpoint-plan-valid"], "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z", "total_units": 4, "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1", "document_id": "doc-1", "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical", "canonical_text_hash": TEXT_HASH, "nodes": deepcopy(NODES),
        "narrative_unit_assets": bindings,
        "template_asset_id": "asset-template", "template_asset_hash": hashlib.sha256(host.assets["asset-template"]).hexdigest(),
        "template_binding": deepcopy(TEMPLATE_BINDING), "plan_id": "plan-validated-valid", "plan_version": 1,
        "hierarchy": [
            {"node_id": "book-1", "parent_node_id": None, "level": "book", "title": "全书", "unit_ids": ["unit-1"], "order": 0},
            {"node_id": "volume-1", "parent_node_id": "book-1", "level": "volume", "title": "第一卷", "unit_ids": ["unit-2"], "order": 1},
            {"node_id": "chapter-1", "parent_node_id": "volume-1", "level": "chapter", "title": "第一章", "unit_ids": ["unit-3"], "order": 2},
            {"node_id": "plot-unit-1", "parent_node_id": "chapter-1", "level": "plot_unit", "title": "第一单元", "unit_ids": ["unit-4"], "order": 3},
        ],
    }
    bundle = NarrativeAnalysisPlugin().run(request, host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[bundle["items"][0]["payload_asset_id"]].decode("utf-8"))
    assert payload["schema"] == "narrative-plan/v1" and payload["immutable"] is True


def synthesis_request(host: Host) -> dict:
    _, plan_id, plan_hash, unit_id, unit_hash = seed_plan_and_unit(host, child_receipt_id="child-receipt-1")
    child_snapshot = "4" * 64
    child_item = {
        "schema": "candidate-item/v1",
        "item_id": "child-candidate-1",
        "item_kind": "relation_set",
        "target": {"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "child-relation-1"},
        "mutation": {"mode": "relation_patch", "payload_schema": "narrative-unit/v1", "payload_hash": unit_hash},
        "payload_asset_id": unit_id,
        "base": {"revision_id": "rev-1", "content_hash": TEXT_HASH},
        "write_set": [{"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "child-relation-1", "revision_id": "rev-1", "content_hash": TEXT_HASH}],
        "parent_candidate_ids": [],
        "source_refs": [{"workspace_id": "ws-1", "source_type": "canonical_revision", "source_id": "doc-1", "revision_or_hash": "rev-1"}],
        "status": "complete",
    }
    child_bundle = {
        "schema": "result-bundle/v1", "contract_id": "candidate-batch/v1", "bundle_id": "child-bundle-1",
        "bundle_type": "candidate_batch",
        "producer": {"plugin_id": "com.plotpilot.novelagent.child", "release_id": "4" * 64,
                      "capability_id": "analysis.narrative.unit.extract/v1", "job_id": "child-job-1",
                      "step_id": "child-step-1", "attempt_id": "child-attempt-1", "lease_epoch": 1},
        "input_snapshot_hash": child_snapshot, "items": [child_item],
        "warnings": [], "partial": False, "provenance_receipt_id": "child-receipt-1", "skill_chain_result_refs": [],
    }
    child_bundle_id, child_bundle_hash = host.seed_json("asset-child-bundle-1", child_bundle)
    invocation = {
        "schema": "broker-invocation/v1",
        "invocation_id": "invocation-1",
        "parent_job_id": "job-synthesis-1",
        "parent_step_id": "step-synthesis-1",
        "parent_attempt_id": "attempt-synthesis-1",
        "invoke_operation_key": "invoke-1",
        "binding_id": "binding-1",
        "input_asset_id": "asset-child-input",
        "input_hash": hashlib.sha256(host.assets["asset-child-input"]).hexdigest(),
        "parameters_asset_id": "asset-child-params",
        "parameters_hash": hashlib.sha256(host.assets["asset-child-params"]).hexdigest(),
        "expected_result_contract": "candidate-batch/v1",
        "required": True,
        "propagate_cancel": True,
    }
    invocation_id, invocation_hash = host.seed_json("asset-broker-invocation-1", invocation)
    child_record = {
        "child_job_id": "child-job-1", "parent_job_id": "job-synthesis-1", "parent_step_id": "step-synthesis-1",
        "parent_attempt_id": "attempt-synthesis-1", "invoke_operation_key": "invoke-1", "binding_id": "binding-1",
        "broker_invocation_asset_id": invocation_id, "broker_invocation_hash": invocation_hash,
        "child_run_snapshot_asset_id": "asset-child-snapshot-1", "child_run_snapshot_hash": child_snapshot,
        "result_contract": "candidate-batch/v1", "required": True, "propagate_cancel": True, "state": "succeeded",
        "result_bundle_asset_id": child_bundle_id, "provenance_receipt_id": "child-receipt-1",
    }
    child_record_id, child_record_hash = host.seed_json("asset-child-record-1", child_record)
    return {
        "schema": "analysis.narrative.synthesize-request/v1", "capability_id": CAPABILITY_SYNTHESIZE,
        "operation_key": CAPABILITY_SYNTHESIZE, "operation": "run", "job_id": "job-synthesis-1",
        "step_id": "step-synthesis-1", "attempt_id": "attempt-synthesis-1", "worker_run_id": "worker-synthesis-1",
        "lease_epoch": 1, "checkpoint_ids": ["checkpoint-synthesis-1"], "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z", "total_units": 1, "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1", "document_id": "doc-1", "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical", "canonical_text_hash": TEXT_HASH, "nodes": deepcopy(NODES),
        "plan_asset_id": plan_id, "plan_asset_hash": plan_hash, "model_profile_revision_id": "model-profile-1",
        "source_bindings": [{"binding_id": "binding-1", "broker_invocation_asset_id": invocation_id,
                             "broker_invocation_hash": invocation_hash, "child_record_asset_id": child_record_id,
                             "child_record_asset_hash": child_record_hash, "result_bundle_asset_id": child_bundle_id,
                             "result_bundle_hash": child_bundle_hash}],
        "broker_invocations": [{"binding_id": "binding-1", "input_asset_id": "asset-child-input",
                                 "parameters_asset_id": "asset-child-params", "expected_result_contract": "candidate-batch/v1",
                                 "propagate_cancel": True}],
    }


def test_synthesis_runtime_preserves_broker_child_receipt_and_per_source_evidence() -> None:
    host = Host()
    plugin = NarrativeAnalysisPlugin()
    bundle = plugin.run(synthesis_request(host), host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[bundle["items"][0]["payload_asset_id"]].decode("utf-8"))
    assert payload["schema"] == "narrative-synthesis/v1"
    assert payload["broker_children"][0]["provenance_receipt_id"] == "child-receipt-1"
    assert payload["source_attributions"][0]["child_receipt_id"] == "child-receipt-1"
    assert payload["evidence_spans"][0]["evidence_span_id"] == "span-1"
    assert plugin.last_child_receipt_ids == ["child-receipt-1"]
    assert "child-receipt-1" in plugin.last_receipt["parent_receipt_ids"]
    verify_provenance_receipt(plugin.last_receipt)


def projection_values(host: Host) -> tuple[dict, dict, dict, dict]:
    _, plan_id, plan_hash, unit_id, unit_hash = seed_plan_and_unit(host, child_receipt_id="child-receipt-1")
    unit = json.loads(host.assets[unit_id].decode("utf-8"))
    child = {"binding_id": "binding-1", "child_job_id": "child-job-1", "child_run_snapshot_hash": "4" * 64,
             "result_contract": "candidate-batch/v1", "result_bundle_asset_id": "asset-child-bundle-1",
             "result_bundle_hash": "5" * 64, "provenance_receipt_id": "child-receipt-1",
             "broker_invocation_asset_id": "asset-broker-1", "broker_invocation_hash": "6" * 64}
    synthesis = {
        "schema": "narrative-synthesis/v1", "synthesis_id": "synthesis-1", "title": "综合", "summary": "闭包",
        "plan_ref": {"plan_id": "plan-1", "payload_asset_id": plan_id, "payload_hash": plan_hash},
        "sections": [{"section_id": "section-1", "title": "第一节", "summary": "转折", "unit_ids": ["unit-1"],
                      "evidence_span_ids": ["span-1"], "source_attribution_ids": ["attribution-1"]}],
        "evidence_spans": unit["evidence_spans"], "source_attributions": unit["source_attributions"],
        "broker_children": [child],
    }
    synthesis_id, synthesis_hash = host.seed_json("asset-synthesis-1", synthesis)
    narrative_identity = json.loads(
        (ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "identity.json").read_text(encoding="utf-8")
    )
    parent_item = {
        "schema": "candidate-item/v1",
        "item_id": "narrative-synthesis-candidate-1",
        "item_kind": "relation_set",
        "target": {"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "narrative-synthesis-relation-1"},
        "mutation": {"mode": "relation_patch", "payload_schema": "narrative-synthesis/v1", "payload_hash": synthesis_hash},
        "payload_asset_id": synthesis_id,
        "base": {"revision_id": "rev-1", "content_hash": TEXT_HASH},
        "write_set": [{"workspace_id": "ws-1", "entity_kind": "relation_set", "entity_id": "narrative-synthesis-relation-1", "revision_id": "rev-1", "content_hash": TEXT_HASH}],
        "parent_candidate_ids": [],
        "source_refs": [{"workspace_id": "ws-1", "source_type": "canonical_revision", "source_id": "doc-1", "revision_or_hash": "rev-1"}],
        "status": "complete",
    }
    parent_bundle = {
        "schema": "result-bundle/v1",
        "contract_id": "candidate-batch/v1",
        "bundle_id": "narrative-result-bundle-1",
        "bundle_type": "candidate_batch",
        "producer": {
            "plugin_id": narrative_identity["plugin_id"],
            "release_id": narrative_identity["release_id"],
            "capability_id": CAPABILITY_SYNTHESIZE,
            "job_id": "job-synthesis-1",
            "step_id": "step-synthesis-1",
            "attempt_id": "attempt-synthesis-1",
            "lease_epoch": 1,
        },
        "input_snapshot_hash": SNAPSHOT_HASH,
        "items": [parent_item],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "source-receipt-1",
        "skill_chain_result_refs": [],
    }
    parent_bundle_id, parent_bundle_hash = host.seed_json("asset-source-bundle-1", parent_bundle)
    request = {
        "schema": "outline.projection.render-request/v1", "capability_id": "outline.projection.render/v1",
        "operation_key": "outline.projection.render/v1", "operation": "run", "job_id": "job-projection-1",
        "step_id": "step-projection-1", "attempt_id": "attempt-projection-1", "worker_run_id": "worker-projection-1",
        "lease_epoch": 1, "checkpoint_ids": ["projection-checkpoint-1"], "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z", "total_units": 1, "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1", "source_bundle_asset_id": parent_bundle_id, "source_bundle_hash": parent_bundle_hash,
        "narrative_unit_assets": [{"asset_id": unit_id, "asset_hash": unit_hash}],
        "views": ["tree", "card", "timeline", "relation"],
    }
    return request, unit, synthesis, child


def test_outline_runtime_emits_artifact_and_preserves_cross_capability_receipts() -> None:
    host = Host()
    request, _, synthesis, child = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    plugin = OutlineProjectionPlugin()
    bundle = plugin.run(request, host)
    assert bundle is not None
    verify_result_bundle(bundle, snapshot_workspace_id="ws-1", snapshot_hash_value=SNAPSHOT_HASH)
    payload = json.loads(host.assets[bundle["items"][0]["payload_asset_id"]].decode("utf-8"))
    assert payload["evidence_spans"] == synthesis["evidence_spans"]
    assert payload["source_attributions"] == synthesis["source_attributions"]
    assert payload["broker_children"] == [child]
    assert payload["authority"]["creates_second_authority"] is False
    assert payload["authority"]["free_form_canvas"] is False
    assert plugin.last_receipt["parent_receipt_ids"] == ["source-receipt-1", "child-receipt-1"]
    assert host.completion_calls[-1]["outcome"] == "succeeded"


def _seed_source_receipt(host: Host, request: dict, synthesis: dict) -> tuple[str, str]:
    parent_bundle = json.loads(host.assets[request["source_bundle_asset_id"]].decode("utf-8"))
    producer = parent_bundle["producer"]
    receipt = {
        "schema": "provenance-receipt/v1",
        "receipt_id": "source-receipt-1",
        "plugin_id": "com.plotpilot.novelagent.narrative-analysis",
        "release_id": producer["release_id"],
        "package_hash": json.loads((ROOT / "plugins" / "narrative-analysis" / "narrative_analysis" / "identity.json").read_text(encoding="utf-8"))["package_hash"],
        "capability_id": CAPABILITY_SYNTHESIZE,
        "job_id": producer["job_id"],
        "step_id": producer["step_id"],
        "attempt_id": producer["attempt_id"],
        "lease_epoch": producer["lease_epoch"],
        "run_snapshot_hash": request["run_snapshot_hash"],
        "bundle_id": parent_bundle["bundle_id"],
        "bundle_hash": hash_jcs("result-bundle/v1", parent_bundle),
        "parent_receipt_ids": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": [],
        "created_at": request["created_at"],
    }
    receipt["receipt_hash"] = hash_jcs("provenance-receipt/v1", receipt)
    return host.seed_json("asset-source-receipt-1", receipt)


def test_outline_runtime_verifies_immediate_source_receipt_binding() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    bundle = OutlineProjectionPlugin().run(request, host)
    assert bundle is not None and bundle["contract_id"] == "artifact-bundle/v1"

    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": "f" * 64,
    })
    failed = OutlineProjectionPlugin().run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_runtime_negative_source_hash_is_failure_result() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    request["source_bundle_hash"] = "f" * 64
    bundle = OutlineProjectionPlugin().run(request, host)
    assert bundle is not None
    assert bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["items"][0]["status"] == "failed"
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_runtime_cancel_after_checkpoint_returns_no_artifact() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = OutlineProjectionPlugin()
    outcome: dict[str, object] = {}

    def worker() -> None:
        outcome["bundle"] = plugin.run(request, host)

    thread = threading.Thread(target=worker)
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancel = deepcopy(request)
    cancel["operation"] = "cancel"
    assert plugin.run(cancel, host) == {"accepted": True, "worker_run_id": "worker-projection-1"}
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["bundle"] is None
    assert host.completion_calls[-1]["outcome"] == "cancelled"


@pytest.mark.parametrize("tamper", ["delete", "replace"])
def test_model_request_binds_exact_canonical_text_and_rejects_body_tampering(tamper: str) -> None:
    host = Host()
    plugin = NarrativeAnalysisPlugin()
    good = plugin.run(unit_request(), host)
    assert good is not None
    assert host.model_requests[-1]["canonical_text"] == TEXT
    assert host.model_requests[-1]["canonical_text_hash"] == TEXT_HASH

    tampered_host = Host()
    tampered_host.tamper_model_request = tamper
    failed = NarrativeAnalysisPlugin().run(unit_request(), tampered_host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert tampered_host.completion_calls[-1]["outcome"] == "failed"
    # The persisted request is rejected before host.model.invoke/v1 can see a
    # deletion/replacement, so there is no provider receipt to trust.
    assert tampered_host.model_requests == []


def _update_synthesis_binding_hash(request: dict, field: str, value: str) -> None:
    request["source_bindings"][0][field] = value


@pytest.mark.parametrize("state,expected", [("partial", True), ("succeeded", False)])
def test_synthesis_parent_outcome_is_derived_from_verified_child_bundle(state: str, expected: bool) -> None:
    host = Host()
    request = synthesis_request(host)
    child_bundle = json.loads(host.assets["asset-child-bundle-1"].decode("utf-8"))
    child_bundle["partial"] = True
    child_bundle["items"][0]["status"] = "partial"
    _, child_bundle_hash = host.seed_json("asset-child-bundle-1", child_bundle)
    _update_synthesis_binding_hash(request, "result_bundle_hash", child_bundle_hash)
    child_record = json.loads(host.assets["asset-child-record-1"].decode("utf-8"))
    child_record["state"] = state
    _, child_record_hash = host.seed_json("asset-child-record-1", child_record)
    _update_synthesis_binding_hash(request, "child_record_asset_hash", child_record_hash)

    plugin = NarrativeAnalysisPlugin()
    bundle = plugin.run(request, host)
    if expected:
        assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
        assert bundle["partial"] is True
        assert bundle["items"][0]["status"] == "partial"
        verify_result_bundle(bundle, snapshot_workspace_id="ws-1", snapshot_hash_value=SNAPSHOT_HASH)
    else:
        assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
        assert bundle["items"][0]["code"] == "BROKER_INVALID"
        assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_source_receipt_must_bind_exact_synthesis_asset_hash() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, _ = _seed_source_receipt(host, request, synthesis)
    receipt = json.loads(host.assets[receipt_id].decode("utf-8"))
    receipt["bundle_hash"] = "0" * 64
    receipt["receipt_hash"] = hash_jcs("provenance-receipt/v1", receipt)
    receipt_id, receipt_hash = host.seed_json("asset-source-receipt-1", receipt)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    failed = OutlineProjectionPlugin().run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["items"][0]["code"] == "SOURCE_CLOSURE_MISSING"
    assert host.completion_calls[-1]["outcome"] == "failed"


class _MalformedTerminalHost(Host):
    def call(self, method: str, params: dict) -> dict:
        if method == "host.job.complete/v1":
            self.completion_calls.append(dict(params))
            # Outline sets its terminal fence before this RPC.  A malformed ACK
            # must therefore raise once and must never be converted into a
            # second failed terminal by catch-all handling.
            return {"accepted": True}
        return super().call(method, params)


def test_outline_formal_main_has_single_terminal_fence_on_malformed_ack() -> None:
    host = _MalformedTerminalHost()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    with pytest.raises(OutlineTerminalContractError):
        outline_main(request, host)
    assert len(host.completion_calls) == 1


def test_outline_formal_main_cancel_and_terminal_receipts_are_verified() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    outcome: dict[str, object] = {}

    def worker() -> None:
        outcome["bundle"] = outline_main(request, host)

    thread = threading.Thread(target=worker)
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancel = deepcopy(request)
    cancel["operation"] = "cancel"
    for field in ("source_receipt_id", "source_receipt_asset_id", "source_receipt_asset_hash"):
        cancel.pop(field, None)
    assert outline_main(cancel, host) == {"accepted": True, "worker_run_id": "worker-projection-1"}
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["bundle"] is None
    assert host.completion_calls[-1]["outcome"] == "cancelled"
    assert isinstance(host.completion_calls[-1].get("terminal_detail_asset_id"), str)

    # A failed Attempt uses the public diagnostic/partial profile and its
    # terminal receipt remains independently verifiable.
    failed_host = Host()
    failed_request, _, failed_synthesis, _ = projection_values(failed_host)
    source_id, source_hash = _seed_source_receipt(failed_host, failed_request, failed_synthesis)
    failed_request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": source_id,
        "source_receipt_asset_hash": source_hash,
        "source_bundle_hash": "f" * 64,
    })
    failed_bundle = OutlineProjectionPlugin().run(failed_request, failed_host)
    assert failed_bundle is not None and failed_bundle["contract_id"] == "diagnostic-bundle/v1"
    verify_attempt_result(
        failed_bundle,
        attempt_state="failed",
        snapshot_workspace_id="ws-1",
        snapshot_hash_value=SNAPSHOT_HASH,
    )


@pytest.mark.parametrize("tamper", ["purpose", "beat", "constraints", "replacement"])
def test_template_semantic_replacement_fails_before_model_or_stage(tamper: str) -> None:
    """The installed Narrative code must not treat caller bytes as Data authority."""
    host = Host()
    template = deepcopy(DATA_TEMPLATE)
    selected = template["templates"][0]
    if tamper == "purpose":
        selected["beats"][0]["purpose"] = "篡改后的目的"
    elif tamper == "beat":
        selected["beats"][0]["beat_id"] = "foreign-beat"
    elif tamper == "constraints":
        selected["constraints"]["ordered"] = False
    else:
        template["templates"] = [
            {
                **selected,
                "template_id": "foreign-template",
            }
        ]
    asset_id, asset_hash = host.seed_json("asset-template-tampered", template)
    request = unit_request()
    request["plot_template_asset_id"] = asset_id
    request["plot_template_asset_hash"] = asset_hash

    failed = NarrativeAnalysisPlugin().run(request, host)

    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["items"][0]["code"] == "TEMPLATE_INVALID"
    # Validation happens before host.model.invoke/v1 and before any candidate
    # stage; a failure Result is still emitted through the public terminal path.
    assert host.model_requests == []
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


def _valid_plan_compile_request(host: Host, *, template_binding: dict | None = None) -> dict:
    """Build a schema-valid four-level plan request for identity negatives."""
    bindings: list[dict[str, str]] = []
    for index in range(1, 5):
        unit = valid_unit()
        unit["unit_id"] = f"unit-template-{index}"
        unit["order"] = index - 1
        unit["evidence_spans"][0]["evidence_span_id"] = f"span-template-{index}"
        unit["source_attributions"][0]["attribution_id"] = f"attribution-template-{index}"
        unit["source_attributions"][0]["evidence_span_ids"] = [f"span-template-{index}"]
        asset_id, asset_hash = host.seed_json(f"asset-template-unit-{index}", unit)
        bindings.append({"asset_id": asset_id, "asset_hash": asset_hash})
    return {
        "schema": "analysis.narrative.plan.compile-request/v1",
        "capability_id": CAPABILITY_PLAN_COMPILE,
        "operation_key": CAPABILITY_PLAN_COMPILE,
        "operation": "validate",
        "job_id": "job-template-negative",
        "step_id": "step-template-negative",
        "attempt_id": "attempt-template-negative",
        "worker_run_id": "worker-template-negative",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-template-negative"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 4,
        "run_snapshot_hash": SNAPSHOT_HASH,
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "source_revision_id": "rev-1",
        "canonical_asset_id": "asset-canonical",
        "canonical_text_hash": TEXT_HASH,
        "nodes": deepcopy(NODES),
        "narrative_unit_assets": bindings,
        "template_asset_id": "asset-template",
        "template_asset_hash": hashlib.sha256(host.assets["asset-template"]).hexdigest(),
        "template_binding": deepcopy(TEMPLATE_BINDING if template_binding is None else template_binding),
        "plan_id": "plan-template-negative",
        "plan_version": 1,
        "hierarchy": [
            {"node_id": "book-template", "parent_node_id": None, "level": "book", "title": "全书", "unit_ids": ["unit-template-1"], "order": 0},
            {"node_id": "volume-template", "parent_node_id": "book-template", "level": "volume", "title": "第一卷", "unit_ids": ["unit-template-2"], "order": 1},
            {"node_id": "chapter-template", "parent_node_id": "volume-template", "level": "chapter", "title": "第一章", "unit_ids": ["unit-template-3"], "order": 2},
            {"node_id": "plot-unit-template", "parent_node_id": "chapter-template", "level": "plot_unit", "title": "第一单元", "unit_ids": ["unit-template-4"], "order": 3},
        ],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_release_id", "a" * 64),
        ("package_hash", "b" * 64),
        ("data_plugin_id", "com.foreign.plot-template"),
        ("template_id", "foreign-template"),
    ],
)
def test_plan_rejects_foreign_template_identity_before_candidate_stage(field: str, value: str) -> None:
    host = Host()
    binding = deepcopy(TEMPLATE_BINDING)
    binding[field] = value
    failed = NarrativeAnalysisPlugin().run(_valid_plan_compile_request(host, template_binding=binding), host)

    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["items"][0]["code"] == "TEMPLATE_INVALID"
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_main_cancel_ignores_run_only_receipt_and_rejects_every_foreign_binding() -> None:
    """A schema-valid cancel must match the live run, not merely its worker ID."""
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    source_id, source_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": source_id,
        "source_receipt_asset_hash": source_hash,
    })
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    outcome: dict[str, object] = {}

    def worker() -> None:
        outcome["bundle"] = outline_main(request, host)

    thread = threading.Thread(target=worker)
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)

    cancel = deepcopy(request)
    cancel["operation"] = "cancel"
    for field in ("source_receipt_id", "source_receipt_asset_id", "source_receipt_asset_hash"):
        cancel.pop(field, None)
    assert outline_main(cancel) == {"accepted": True, "worker_run_id": request["worker_run_id"]}

    foreign_cases = [
        ("job_id", "foreign-job"),
        ("step_id", "foreign-step"),
        ("attempt_id", "foreign-attempt"),
        ("lease_epoch", 2),
        ("run_snapshot_hash", "2" * 64),
        ("checkpoint_ids", ["foreign-checkpoint"]),
        ("provenance_receipt_id", "foreign-receipt"),
        ("source_bundle_asset_id", "foreign-source"),
        ("source_bundle_hash", "3" * 64),
        ("views", ["card"]),
    ]
    for field, value in foreign_cases:
        foreign = deepcopy(cancel)
        foreign[field] = value
        assert outline_main(foreign) == {"accepted": False, "worker_run_id": request["worker_run_id"]}

    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["bundle"] is None
    assert host.stage_calls == []
    assert len(host.completion_calls) == 1
    assert host.completion_calls[0]["outcome"] == "cancelled"
    assert host.completion_calls[0]["result_bundle_asset_id"] is None


def test_outline_main_rejects_cancel_after_terminal_and_keeps_single_success() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    source_id, source_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": source_id,
        "source_receipt_asset_hash": source_hash,
    })
    bundle = outline_main(request, host)
    assert bundle is not None
    cancel = deepcopy(request)
    cancel["operation"] = "cancel"
    for field in ("source_receipt_id", "source_receipt_asset_id", "source_receipt_asset_hash"):
        cancel.pop(field, None)
    assert outline_main(cancel) == {"accepted": False, "worker_run_id": request["worker_run_id"]}
    assert len(host.completion_calls) == 1
    assert host.completion_calls[0]["outcome"] == "succeeded"
    assert host.completion_calls[0]["result_bundle_asset_id"] is not None


@pytest.mark.parametrize("tamper", ["release_id", "package_hash"])
def test_outline_rejects_foreign_narrative_receipt_identity(tamper: str) -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, _ = _seed_source_receipt(host, request, synthesis)
    receipt = json.loads(host.assets[receipt_id].decode("utf-8"))
    receipt[tamper] = "e" * 64
    receipt["receipt_hash"] = hash_jcs("provenance-receipt/v1", receipt)
    receipt_id, receipt_hash = host.seed_json("asset-foreign-narrative-receipt", receipt)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })
    failed = OutlineProjectionPlugin().run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["items"][0]["code"] == "SOURCE_CLOSURE_MISSING"
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_rejects_synthesis_payload_masquerading_as_result_bundle() -> None:
    host = Host()
    request, _, synthesis, _ = projection_values(host)
    receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
    request.update({
        "source_receipt_id": "source-receipt-1",
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
        "source_bundle_asset_id": "asset-synthesis-1",
        "source_bundle_hash": hashlib.sha256(host.assets["asset-synthesis-1"]).hexdigest(),
    })
    failed = OutlineProjectionPlugin().run(request, host)
    assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
    assert failed["items"][0]["code"] == "SOURCE_CLOSURE_MISSING"
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_rejects_foreign_producer_and_payload_bindings() -> None:
    for tamper in ("producer", "payload"):
        host = Host()
        request, _, synthesis, _ = projection_values(host)
        bundle = json.loads(host.assets[request["source_bundle_asset_id"]].decode("utf-8"))
        if tamper == "producer":
            bundle["producer"]["plugin_id"] = "com.foreign.narrative"
        else:
            # Keep a valid Bundle envelope but make its Candidate point at a
            # payload whose declared hash cannot be read as that Asset.
            bundle["items"][0]["mutation"]["payload_hash"] = "f" * 64
        source_id, source_hash = host.seed_json(f"asset-foreign-{tamper}-bundle", bundle)
        request["source_bundle_asset_id"] = source_id
        request["source_bundle_hash"] = source_hash
        receipt_id, receipt_hash = _seed_source_receipt(host, request, synthesis)
        request.update({
            "source_receipt_id": "source-receipt-1",
            "source_receipt_asset_id": receipt_id,
            "source_receipt_asset_hash": receipt_hash,
        })
        failed = OutlineProjectionPlugin().run(request, host)
        assert failed is not None and failed["contract_id"] == "diagnostic-bundle/v1"
        assert failed["items"][0]["code"] in {"SOURCE_CLOSURE_MISSING", "ASSET_HASH_MISMATCH"}
        assert host.completion_calls[-1]["outcome"] == "failed"


def test_outline_accepts_receipt_from_actual_narrative_runtime_result_bundle() -> None:
    """The cross-capability seam must accept the runtime's real Bundle/receipt."""
    host = Host()
    narrative_request = synthesis_request(host)
    narrative_plugin = NarrativeAnalysisPlugin()
    narrative_bundle = narrative_plugin.run(narrative_request, host)
    assert narrative_bundle is not None and narrative_plugin.last_receipt is not None
    narrative_raw = canonical_bytes(narrative_bundle)
    narrative_hash = hashlib.sha256(narrative_raw).hexdigest()
    narrative_asset_id = "asset-" + narrative_hash[:40]
    assert host.assets[narrative_asset_id] == narrative_raw

    request, _, synthesis, _ = projection_values(host)
    request["source_bundle_asset_id"] = narrative_asset_id
    request["source_bundle_hash"] = narrative_hash
    receipt_id, receipt_hash = host.seed_json("asset-runtime-narrative-receipt", narrative_plugin.last_receipt)
    request.update({
        "source_receipt_id": narrative_plugin.last_receipt["receipt_id"],
        "source_receipt_asset_id": receipt_id,
        "source_receipt_asset_hash": receipt_hash,
    })

    outline_bundle = OutlineProjectionPlugin().run(request, host)

    assert outline_bundle is not None and outline_bundle["contract_id"] == "artifact-bundle/v1"
    assert host.completion_calls[-1]["outcome"] == "succeeded"
