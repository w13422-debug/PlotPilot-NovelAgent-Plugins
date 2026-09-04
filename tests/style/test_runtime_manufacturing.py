from __future__ import annotations

import base64
import hashlib
import json
import threading
from copy import deepcopy

import pytest

from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.verifier import (
    verify_checkpoint,
    verify_provenance_receipt,
    verify_result_bundle,
)
from style_manufacturing.contract import (
    exact_release_eligible,
    validate_qualification_receipt,
)
from style_manufacturing.contract import SEMVER_RE as MANUFACTURING_SEMVER_RE
from style_runtime.contract import SEMVER_RE as RUNTIME_SEMVER_RE
from style_manufacturing.runtime import (
    CAPABILITY_MANUFACTURE,
    CAPABILITY_PACKAGE,
    CAPABILITY_QUALIFY,
    StyleManufacturingPlugin,
    _writer_text_is_qualifying,
)
from test_contract_semantics import qualification, style_pack


class StyleHost:
    """Strict public HostPort fake with durable upload and stage replay."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.uploads: dict[str, bytearray] = {}
        self.upload_results: dict[tuple[str, int], tuple[str, dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.event_calls: list[dict] = []
        self.checkpoint_calls: list[dict] = []
        self.stage_calls: list[dict] = []
        self.completion_calls: list[dict] = []
        self.logical_stage_results: dict[str, dict] = {}
        self.model_outputs: dict[str, dict] = {
            "style.manufacture-model-response/v1": {
                "schema": "style.manufacture-model-response/v1",
                "features": {
                    "voice": sorted(["精确", "克制"], key=lambda x: x.encode("utf-8")),
                    "rhythm": ["短长交替"],
                    "syntax": ["动作前置"],
                    "imagery": ["冷色意象"],
                    "taboos": ["空泛总结"],
                },
                "constraints": sorted(["禁止模仿署名", "保留事实"], key=lambda x: x.encode("utf-8")),
                "exemplar_hashes": ["9" * 64, "a" * 64],
                "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
            },
            "style.qualify.writer-model-response/v1": {
                "schema": "style.qualify.writer-model-response/v1",
                "cases": [
                    {"case_id": f"blind-case-{index}", "output_text": f"匿名输出 {index}"}
                    for index in range(1, 4)
                ],
            },
            "style.qualify.reviewer-model-response/v1": {
                "schema": "style.qualify.reviewer-model-response/v1",
                "cases": [
                    {"case_id": f"blind-case-{index}", "score_0_100": 90, "passed": True}
                    for index in range(1, 4)
                ],
            },
        }
        self.block_checkpoint = False
        self.checkpoint_entered = threading.Event()
        self.checkpoint_release = threading.Event()
        self.checkpoint_release.set()
        self._event_seq = 0
        self._model_receipt_seq = 0

    @staticmethod
    def assert_keys(value: dict, expected: set[str]) -> None:
        assert set(value) == expected, (sorted(value), sorted(expected))

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def seed_bytes(self, asset_id: str, data: bytes) -> tuple[str, str]:
        self.assets[asset_id] = data
        return asset_id, hashlib.sha256(data).hexdigest()

    def seed_json(self, asset_id: str, value) -> tuple[str, str]:
        return self.seed_bytes(asset_id, canonical_bytes(value))

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
            key = (params["upload_id"], params["offset"])
            request_hash = hashlib.sha256(canonical_bytes(params)).hexdigest()
            previous = self.upload_results.get(key)
            if previous is not None:
                assert previous[0] == request_hash
                return deepcopy(previous[1])
            chunk = base64.b64decode(params["base64_chunk"], validate=True)
            assert hashlib.sha256(chunk).hexdigest() == params["chunk_hash"]
            target = self.uploads.setdefault(params["upload_id"], bytearray())
            assert params["offset"] == len(target)
            target.extend(chunk)
            asset_id = None
            if params["final"]:
                data = bytes(target)
                assert hashlib.sha256(data).hexdigest() == params["expected_hash"]
                asset_id = "asset-" + hashlib.sha256(data).hexdigest()[:40]
                self.assets[asset_id] = data
            result = {
                "upload_id": params["upload_id"],
                "accepted_bytes": len(target),
                "completed": bool(params["final"]),
                "asset_id": asset_id,
            }
            self.upload_results[key] = (request_hash, deepcopy(result))
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
            request = json.loads(self.assets[params["request_asset_id"]].decode("utf-8"))
            output = self.model_outputs[request["output_schema"]]
            self._model_receipt_seq += 1
            response_asset_id, response_hash = self.seed_json(
                f"asset-model-{self._model_receipt_seq}", output
            )
            response = {
                "state": "received",
                "response_asset_id": response_asset_id,
                "receipt_id": f"model-receipt-{self._model_receipt_seq}",
                "uncertainty": None,
            }
            # The manufacturing fixture models the Core HostPort's immutable
            # receipt projection.  Style-runtime deliberately keeps the
            # frozen public RPC response shape and does not consume this
            # plugin-specific extension.
            if request.get("schema", "").startswith(("style.manufacture", "style.qualify")):
                profile = request.get("model_profile_revision_id", params.get("model_profile_revision_id"))
                snapshot_hash = request.get("run_snapshot_hash", "0" * 64)
                attempt_ids = request.get("attempt_ids")
                if not isinstance(attempt_ids, list) or not attempt_ids:
                    attempt_ids = ["attempt:fixture-model"]
                response["model_receipt"] = {
                    "receipt_id": response["receipt_id"],
                    "model_profile_revision_id": profile,
                    "route_id": request.get("route_id", "route:fixture"),
                    "attempt_id": attempt_ids[0],
                    "run_snapshot_hash": snapshot_hash,
                    "request_asset_id": params["request_asset_id"],
                    "request_asset_hash": hashlib.sha256(self.assets[params["request_asset_id"]]).hexdigest(),
                    "response_asset_id": response_asset_id,
                    "response_asset_hash": response_hash,
                }
            return response
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


def common(capability: str, *, operation: str = "run") -> dict:
    schemas = {
        CAPABILITY_MANUFACTURE: "style.manufacture-request/v1",
        CAPABILITY_QUALIFY: "style.qualify-request/v1",
        CAPABILITY_PACKAGE: "style.data-plugin.package-request/v1",
    }
    return {
        "schema": schemas[capability],
        "capability_id": capability,
        "operation_key": capability,
        "operation": operation,
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "worker_run_id": "worker-1",
        "lease_epoch": 1,
        "checkpoint_ids": ["checkpoint-1"],
        "provenance_receipt_id": "receipt-test",
        "created_at": "2026-08-30T00:00:00Z",
        "total_units": 1,
        "run_snapshot_hash": "1" * 64,
        "workspace_id": "ws-1",
    }


def manufacture_request(host: StyleHost, *, operation: str = "run") -> dict:
    source_id, source_hash = host.seed_bytes("asset-source", "原作样本".encode())
    return {
        **common(CAPABILITY_MANUFACTURE, operation=operation),
        "style_pack_id": "style-pack:author-a",
        "style_version": "1.0.0",
        "source_asset_id": source_id,
        "source_asset_hash": source_hash,
        "source_revision_id": "revision:source-1",
        "target_entity_id": "style:author-a",
        "target_base_revision_id": "revision:base-1",
        "target_base_content_hash": "2" * 64,
        "model_profile_revision_id": "model-profile:manufacture",
        "route_id": "route:manufacture",
        "prompt_hash": "3" * 64,
        "output_schema_hash": "4" * 64,
        "chunk_plan_hash": "5" * 64,
        "lexicon_asset_refs": [],
        "provider_attempt_ids": ["attempt:provider-1"],
    }


def qualify_request(host: StyleHost, *, same_path: bool = False) -> dict:
    pack_id, pack_hash = host.seed_json("asset-style-pack", style_pack())
    rubric_id, rubric_hash = host.seed_json("asset-rubric", {"schema": "quality-rubric/v1"})
    return {
        **common(CAPABILITY_QUALIFY),
        "style_pack_asset_id": pack_id,
        "style_pack_asset_hash": pack_hash,
        "rubric_asset_id": rubric_id,
        "rubric_asset_hash": rubric_hash,
        "manufacturing_route_id": "route:manufacture",
        "writer_route_id": "route:writer",
        "reviewer_route_id": "route:writer" if same_path else "route:reviewer",
        "manufacturing_attempt_ids": ["attempt:manufacture-1", "attempt:manufacture-2"],
        "writer_attempt_ids": ["attempt:writer"],
        "reviewer_attempt_ids": ["attempt:reviewer"],
        "writer_model_profile_revision_id": "model-profile:writer",
        "reviewer_model_profile_revision_id": "model-profile:reviewer",
    }


def package_request(host: StyleHost, *, pack: dict | None = None, receipt: dict | None = None) -> dict:
    selected = pack or style_pack()
    selected_receipt = receipt or qualification(selected)
    pack_id, pack_hash = host.seed_json("asset-package-style", selected)
    receipt_id, receipt_hash = host.seed_json("asset-package-qualification", selected_receipt)
    return {
        **common(CAPABILITY_PACKAGE, operation="validate"),
        "style_pack_asset_id": pack_id,
        "style_pack_asset_hash": pack_hash,
        "qualification_receipt_asset_id": receipt_id,
        "qualification_receipt_asset_hash": receipt_hash,
        "data_plugin_id": "com.plotpilot.data.style.author-a",
        "data_version": "1.0.0",
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


def test_manufacture_closes_candidate_attempt_checkpoint_receipt_and_terminal_result() -> None:
    host = StyleHost()
    plugin = StyleManufacturingPlugin()
    request = manufacture_request(host)
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "candidate-batch/v1"
    assert bundle["bundle_type"] == "candidate_batch" and bundle["partial"] is False
    assert len(bundle["items"]) == 1
    verify_result_bundle(
        bundle,
        snapshot_workspace_id=request["workspace_id"],
        snapshot_hash_value=request["run_snapshot_hash"],
    )
    assert plugin.last_checkpoint is not None
    verify_checkpoint(
        plugin.last_checkpoint["checkpoint"], expected_snapshot_hash=request["run_snapshot_hash"]
    )
    assert plugin.last_receipt is not None
    verify_provenance_receipt(plugin.last_receipt)
    assert plugin.last_receipt["model_receipt_ids"] == ["model-receipt-1"]
    assert plugin.last_receipt["staged_items"] == [bundle["items"][0]["item_id"]]
    assert len(host.checkpoint_calls) == len(host.stage_calls) == len(host.completion_calls) == 1
    assert host.completion_calls[0]["outcome"] == "succeeded"


def test_resume_replays_exact_checkpoint_without_reinvoking_model_and_rejects_state_tamper() -> None:
    host = StyleHost()
    plugin = StyleManufacturingPlugin()
    request = manufacture_request(host)
    first = plugin.run(request, host)
    assert first is not None and plugin.last_checkpoint is not None
    checkpoint = deepcopy(plugin.last_checkpoint)
    model_count = sum(method == "host.model.invoke/v1" for method, _ in host.calls)
    resumed = plugin.run(resume_request(request, checkpoint), host)
    assert resumed == first
    assert sum(method == "host.model.invoke/v1" for method, _ in host.calls) == model_count
    assert host.stage_calls[0]["operation_key"] == host.stage_calls[1]["operation_key"]

    state = json.loads(host.assets[checkpoint["state_asset_id"]].decode("utf-8"))
    state["release_id"] = "0" * 64
    tampered_id, tampered_hash = host.seed_json("asset-tampered-state", state)
    tampered = resume_request(request, checkpoint)
    tampered["resume_state_asset_id"] = tampered_id
    tampered["resume_state_asset_hash"] = tampered_hash
    failure = plugin.run(tampered, host)
    assert failure is not None and failure["contract_id"] == "diagnostic-bundle/v1"
    assert failure["partial"] is True
    assert failure["items"][0]["code"] == "RESUME_INVALID"
    verify_result_bundle(
        failure,
        snapshot_hash_value=request["run_snapshot_hash"],
        attempt_state="failed",
    )


def test_cancel_at_durable_checkpoint_is_legal_and_never_stages_candidate() -> None:
    host = StyleHost()
    host.block_checkpoint = True
    host.checkpoint_release.clear()
    plugin = StyleManufacturingPlugin()
    request = manufacture_request(host)
    result: list[object] = []

    thread = threading.Thread(target=lambda: result.append(plugin.run(request, host)))
    thread.start()
    assert host.checkpoint_entered.wait(timeout=5)
    cancel = {**request, "operation": "cancel"}
    assert plugin.run(cancel, host) is None
    host.checkpoint_release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and result == [None]
    assert host.stage_calls == []
    assert host.completion_calls[-1]["outcome"] == "cancelled"
    assert plugin.last_receipt is not None
    verify_provenance_receipt(plugin.last_receipt)


def test_same_path_qualification_is_rejected_before_any_model_or_candidate_side_effect() -> None:
    host = StyleHost()
    plugin = StyleManufacturingPlugin()
    assert plugin.run(qualify_request(host, same_path=True), host) is None
    assert not any(method == "host.model.invoke/v1" for method, _ in host.calls)
    assert host.stage_calls == [] and host.completion_calls == []


def test_independent_qualification_run_is_diagnostic_only_and_closes_exact_receipt() -> None:
    host = StyleHost()
    plugin = StyleManufacturingPlugin()
    request = qualify_request(host)
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["items"][0]["code"] == "STYLE_QUALIFIED"
    assert bundle["items"][0]["status"] == "complete"
    assert host.stage_calls == []
    assert plugin.last_qualification_receipt is not None
    receipt = validate_qualification_receipt(plugin.last_qualification_receipt)
    pack = style_pack()
    assert exact_release_eligible(pack, receipt) is True
    assert plugin.last_checkpoint is not None and plugin.last_receipt is not None
    verify_checkpoint(plugin.last_checkpoint["checkpoint"], expected_snapshot_hash=request["run_snapshot_hash"])
    verify_provenance_receipt(plugin.last_receipt)
    assert len(plugin.last_receipt["model_receipt_ids"]) == 2
    assert host.completion_calls[-1]["outcome"] == "succeeded"


def test_model_failure_is_a_legal_diagnostic_result_with_receipt_and_failed_terminal() -> None:
    host = StyleHost()
    host.model_outputs["style.manufacture-model-response/v1"] = {
        "schema": "style.manufacture-model-response/v1",
        "features": {},
    }
    plugin = StyleManufacturingPlugin()
    request = manufacture_request(host)
    bundle = plugin.run(request, host)
    assert bundle is not None
    assert bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["bundle_type"] == "diagnostic" and bundle["partial"] is True
    assert bundle["items"][0]["status"] == "failed"
    verify_result_bundle(
        bundle,
        snapshot_hash_value=request["run_snapshot_hash"],
        attempt_state="failed",
    )
    assert plugin.last_receipt is not None
    verify_provenance_receipt(plugin.last_receipt)
    assert host.completion_calls[-1]["outcome"] == "failed"


def test_data_package_validate_accepts_only_exact_qualification_and_keeps_identity_domains_separate() -> None:
    host = StyleHost()
    plugin = StyleManufacturingPlugin()
    request = package_request(host)
    bundle = plugin.run(request, host)
    assert bundle is not None and bundle["contract_id"] == "artifact-bundle/v1"
    assert bundle["items"][0]["artifact_kind"] == "style-data-package/v1"
    assert plugin.last_receipt is None
    assert host.stage_calls == [] and host.completion_calls == []

    exact_pack = style_pack()
    stale_receipt = qualification(exact_pack)
    different = style_pack(version="9.0.0")
    failure = plugin.run(package_request(host, pack=different, receipt=stale_receipt), host)
    assert failure is not None and failure["contract_id"] == "diagnostic-bundle/v1"
    assert failure["items"][0]["status"] == "failed"


@pytest.mark.parametrize(
    "low_information",
    [
        "abcde",
        "qwert",
        "xxxxx",
        "lorem",
        "12345",
        "!!!!!",
        "a1b2c",
        "qwerty qwerty",
        "lorem lorem lorem",
        "river river river",
        "lorem ipsum lorem ipsum",
        "测试 测试 测试",
        "abababababab",
        "lorem ipsum dolor sit amet",
        "qwerty asdf zxcv qwerty",
    ],
)
def test_r1_f001_low_information_writer_outputs_fail_closed(low_information: str) -> None:
    """Short/gibberish outputs cannot become an eligible qualification."""
    host = StyleHost()
    host.model_outputs["style.qualify.writer-model-response/v1"] = {
        "schema": "style.qualify.writer-model-response/v1",
        "cases": [
            {"case_id": f"blind-case-{index}", "output_text": low_information}
            for index in range(1, 4)
        ],
    }
    plugin = StyleManufacturingPlugin()
    bundle = plugin.run(qualify_request(host), host)
    assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
    assert bundle["items"][0]["code"] == "MODEL_OUTPUT_INVALID"
    assert plugin.last_qualification_receipt is None
    assert host.stage_calls == []


def test_r1_f001_quality_gate_requires_the_frozen_case_rubric_and_style_context() -> None:
    sample = "The quiet river crosses the old town at dusk."
    case = {"prompt": "Rewrite the sealed case without changing its facts."}
    rubric = {"schema": "quality-rubric/v1"}
    pack = style_pack()

    assert _writer_text_is_qualifying(sample, case=case, rubric=rubric, style_pack=pack)
    assert not _writer_text_is_qualifying(sample, case=case, rubric=rubric, style_pack=None)
    assert not _writer_text_is_qualifying(sample, case=None, rubric=rubric, style_pack=pack)
    assert not _writer_text_is_qualifying(sample, case=case, rubric=None, style_pack=pack)


@pytest.mark.parametrize(
    "writer_text",
    [
        "匿名输出 1",
        "The quiet river crosses the old town at dusk.",
        "The test scene becomes quiet when the river freezes at dawn.",
        "The word lorem appears once in this otherwise complete explanatory sentence.",
    ],
)
def test_r1_f001_contextual_quality_gate_keeps_informative_samples(writer_text: str) -> None:
    host = StyleHost()
    host.model_outputs["style.qualify.writer-model-response/v1"] = {
        "schema": "style.qualify.writer-model-response/v1",
        "cases": [
            {"case_id": f"blind-case-{index}", "output_text": writer_text}
            for index in range(1, 4)
        ],
    }
    plugin = StyleManufacturingPlugin()
    bundle = plugin.run(qualify_request(host), host)
    assert bundle is not None and bundle["items"][0]["code"] == "STYLE_QUALIFIED"
    assert plugin.last_qualification_receipt is not None
    assert plugin.last_qualification_receipt["automatic_eligible"] is True


def test_r1_f002_model_receipt_requires_host_provenance_and_exact_binding() -> None:
    class NoProvenanceHost(StyleHost):
        def call(self, method: str, params: dict) -> dict:
            response = super().call(method, params)
            if method == "host.model.invoke/v1":
                response = dict(response)
                response.pop("model_receipt", None)
            return response

    class WrongBindingHost(StyleHost):
        def __init__(self, field: str) -> None:
            super().__init__()
            self.field = field

        def call(self, method: str, params: dict) -> dict:
            response = super().call(method, params)
            if method == "host.model.invoke/v1":
                response = dict(response)
                receipt = dict(response["model_receipt"])
                receipt[self.field] = {
                    "run_snapshot_hash": "f" * 64,
                    "model_profile_revision_id": "model-profile:foreign",
                    "route_id": "route:foreign",
                    "attempt_id": "attempt:foreign",
                    "request_asset_id": "asset:foreign",
                    "request_asset_hash": "e" * 64,
                    "response_asset_id": "asset:foreign-output",
                    "response_asset_hash": "d" * 64,
                }[self.field]
                response["model_receipt"] = receipt
            return response

    class ForgedReceiptHost(StyleHost):
        def call(self, method: str, params: dict) -> dict:
            response = super().call(method, params)
            if method == "host.model.invoke/v1":
                response = dict(response)
                response["receipt_id"] = f"model-receipt-{900 + self._model_receipt_seq}"
            return response

    hosts = [NoProvenanceHost(), ForgedReceiptHost()]
    hosts.extend(WrongBindingHost(field) for field in (
        "run_snapshot_hash", "model_profile_revision_id", "route_id",
        "attempt_id", "request_asset_id", "request_asset_hash",
        "response_asset_id", "response_asset_hash",
    ))
    for host in hosts:
        plugin = StyleManufacturingPlugin()
        bundle = plugin.run(qualify_request(host), host)
        assert bundle is not None and bundle["contract_id"] == "diagnostic-bundle/v1"
        assert bundle["items"][0]["code"] == "MODEL_PROVENANCE_INVALID"
        assert plugin.last_qualification_receipt is None
        assert host.stage_calls == []


@pytest.mark.parametrize(
    ("version", "valid"),
    [
        ("1.0.0-0", True),
        ("1.0.0-alpha.1", True),
        ("1.0.0-alpha-01", True),
        ("1.0.0-01", False),
        ("1.0.0-alpha.01", False),
        ("1.0.0-00", False),
    ],
)
def test_r1_f006_semver_prerelease_numeric_zero_rules_are_shared(version: str, valid: bool) -> None:
    assert bool(MANUFACTURING_SEMVER_RE.fullmatch(version)) is valid
    assert bool(RUNTIME_SEMVER_RE.fullmatch(version)) is valid

    host = StyleHost()
    request = package_request(host)
    request["data_version"] = version
    bundle = StyleManufacturingPlugin().run(request, host)
    if valid:
        assert bundle is not None and bundle["contract_id"] == "artifact-bundle/v1"
    else:
        # Invalid request versions are rejected before a run context exists;
        # no artifact or host side effect is permitted.
        assert bundle is None
