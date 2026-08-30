from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "source-cleaning-runtime"
sys.path[:0] = [str(PLUGIN), str(ROOT / "sdk"), str(Path(__file__).parent)]

import source_cleaning_runtime.runtime as runtime  # noqa: E402
from source_cleaning_runtime import (  # noqa: E402
    CAPABILITY_APPLY,
    CAPABILITY_MERGE,
    CAPABILITY_PREVIEW,
    ContractError,
    apply,
    build_review_context,
    make_cleaning_request,
    make_merge_request,
    merge_rules,
    preview,
    sha256_text,
)
from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_capability_descriptor,
    verify_provenance_receipt,
    verify_result_bundle,
)

from rebuild_package import rebuild  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rules() -> dict:
    return json.loads(
        (ROOT / "data/source-cleaning/rules/data/rules.json").read_text(encoding="utf-8")
    )


def _expected() -> dict:
    return json.loads(
        (ROOT / "data/source-cleaning/rules/expected.json").read_text(encoding="utf-8")
    )


def _case() -> dict:
    return json.loads(
        (ROOT / "data/source-cleaning/fixtures/cleaning/golden_cases.json").read_text(encoding="utf-8")
    )["cases"][0]


def _request(case: dict, *, capability: str = CAPABILITY_PREVIEW, review_context: dict | None = None, operation: str = "run") -> dict:
    expected = _expected()
    request = make_cleaning_request(
        capability=capability,
        operation=operation,
        job_id="job-preaudit",
        step_id="step-clean",
        attempt_id="attempt-preaudit",
        lease_epoch=1,
        run_snapshot_hash=sha256_text("preaudit-snapshot"),
        workspace_id="workspace-nap01",
        document_id="document-preaudit",
        revision_id="revision-preaudit",
        base_content_hash=sha256_text(case["source_text"]),
        source_asset_id="source-preaudit",
        source_text=case["source_text"],
        rules=_rules(),
        rules_release_id=expected["release_id"],
        rules_package_hash=expected["package_hash"],
        input_kind=case["input_kind"],
        storage_source_format=case["storage_source_format"],
        review_context=review_context,
    )
    request.update(
        {
            "worker_run_id": "worker-preaudit",
            "checkpoint_ids": ["checkpoint-preaudit-1"],
            "provenance_receipt_id": "receipt-preaudit",
            "created_at": "2026-08-27T00:00:00Z",
            "total_units": 1,
        }
    )
    return request


class FrozenRpcHost:
    """Frozen B0-style host.call double with exact method field profiles."""

    def __init__(self, source_text: str, *, page_size: int = 4, stage_mode: str = "valid", fail_checkpoint_commit: bool = False) -> None:
        self.source_asset = source_text.encode("utf-8")
        self.page_size = page_size
        self.stage_mode = stage_mode
        self.fail_checkpoint_commit = fail_checkpoint_commit
        self.assets: dict[str, bytes] = {"source-preaudit": self.source_asset}
        self.uploads: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.event_seq = 0
        self.stage_calls: list[dict[str, object]] = []
        self.stage_responses: list[dict[str, object]] = []
        self.completions: list[dict[str, object]] = []

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        expected_fields = {
            "host.asset.read/v1": {"asset_id", "offset", "length"},
            "host.asset.create/v1": {"operation_key", "upload_id", "offset", "mime", "total_size", "expected_hash", "chunk_hash", "base64_chunk", "final"},
            "host.asset.upload.status/v1": {"upload_id", "expected_hash"},
            "host.job.event/v1": {"operation_key", "event_type", "payload_asset_id", "local_seq"},
            "host.checkpoint.commit/v1": {"operation_key", "checkpoint_asset_id"},
            "host.candidate.stage/v1": {"operation_key", "result_bundle_asset_id", "input_snapshot_hash"},
            "host.job.complete/v1": {"operation_key", "worker_run_id", "outcome", "result_bundle_asset_id", "candidate_stage_operation_key", "terminal_detail_asset_id", "local_seq"},
        }
        if method not in expected_fields or set(params) != expected_fields[method]:
            raise AssertionError(f"non-frozen RPC signature: {method}: {sorted(params)}")

        if method == "host.asset.read/v1":
            asset = self.assets[str(params["asset_id"])]
            offset = int(params["offset"])
            requested = min(int(params["length"]), self.page_size)
            chunk = asset[offset : offset + requested]
            end = offset + len(chunk)
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": None if end == len(asset) else end,
                "content_hash": _sha(chunk),
            }
        if method == "host.asset.create/v1":
            chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
            if int(params["offset"]) != 0 or params["final"] is not True:
                raise AssertionError("cleaning must use one final Asset upload")
            if len(chunk) != int(params["total_size"]):
                raise AssertionError("upload size mismatch")
            if _sha(chunk) != params["chunk_hash"] or params["expected_hash"] != params["chunk_hash"]:
                raise AssertionError("upload hash mismatch")
            asset_id = f"asset-{_sha(chunk)[:24]}"
            self.assets[asset_id] = chunk
            self.uploads[str(params["upload_id"])] = asset_id
            return {"upload_id": params["upload_id"], "accepted_bytes": len(chunk), "completed": True, "asset_id": asset_id}
        if method == "host.asset.upload.status/v1":
            asset_id = self.uploads[str(params["upload_id"])]
            data = self.assets[asset_id]
            if params["expected_hash"] != _sha(data):
                raise AssertionError("status hash mismatch")
            return {"accepted_bytes": len(data), "completed": True, "asset_id": asset_id}
        if method == "host.job.event/v1":
            self.event_seq += 1
            return {"accepted": True, "job_event_seq": self.event_seq}
        if method == "host.checkpoint.commit/v1":
            if self.fail_checkpoint_commit:
                raise RuntimeError("checkpoint commit failure")
            checkpoint = json.loads(self.assets[str(params["checkpoint_asset_id"])].decode("utf-8"))
            self.event_seq += 1
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": self.event_seq,
            }
        if method == "host.candidate.stage/v1":
            self.stage_calls.append(dict(params))
            self.event_seq += 1
            bundle = json.loads(self.assets[str(params["result_bundle_asset_id"])].decode("utf-8"))
            item_ids = [item["item_id"] for item in bundle["items"]]
            if self.stage_mode == "empty":
                staged_items = []
            elif self.stage_mode == "missing":
                staged_items = None
            elif self.stage_mode == "duplicate":
                staged_items = [{
                    "item_id": item_ids[0],
                    "candidate_id": "candidate-preaudit",
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                }] * 2
            elif self.stage_mode == "mismatch":
                staged_items = [{
                    "item_id": "wrong-item",
                    "candidate_id": "candidate-preaudit",
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                }]
            elif self.stage_mode == "failed":
                staged_items = [{
                    "item_id": item_ids[0],
                    "candidate_id": "candidate-preaudit",
                    "stage_status": "failed",
                    "publication_eligibility": "none",
                }]
            else:
                staged_items = [{
                    "item_id": item_id,
                    "candidate_id": f"candidate-{item_id[:16]}",
                    "stage_status": "created",
                    "publication_eligibility": "review_only",
                } for item_id in item_ids]
            response = {
                "accepted": True,
                "job_event_seq": self.event_seq,
            }
            if staged_items is not None:
                response["staged_items"] = staged_items
            self.stage_responses.append(copy.deepcopy(response))
            return response
        if method == "host.job.complete/v1":
            self.completions.append(dict(params))
            self.event_seq += 1
            outcome = str(params["outcome"])
            return {
                "accepted": True,
                "attempt_state": outcome,
                "step_state": outcome,
                "job_state": outcome,
                "provenance_receipt_id": "receipt-preaudit",
                "job_event_seq": self.event_seq,
                "core_event_high_water": self.event_seq,
            }
        raise AssertionError(f"unexpected RPC: {method}")


class ControllerPreauditTests(unittest.TestCase):
    def test_wheel_omits_identity_and_installed_sidecar_is_required_and_deterministic(self) -> None:
        import subprocess
        import zipfile

        rebuild()
        expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
        wheel = PLUGIN / expected["wheel_path"]
        sidecar = PLUGIN / expected["wheel_import"] / "identity.json"
        outer_identity = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(outer_identity["package_hash"], expected["package_hash"])
        self.assertEqual(outer_identity["release_id"], expected["release_id"])
        with zipfile.ZipFile(wheel) as archive:
            self.assertIsNone(archive.testzip())
            self.assertNotIn("source_cleaning_runtime/identity.json", archive.namelist())

        first = wheel.read_bytes()
        rebuild()
        second = wheel.read_bytes()
        self.assertEqual(first, second)

        env = dict(os.environ)
        env["PYTHONPATH"] = ";".join(
            [
                str(ROOT / "sdk"),
                r"C:\Users\Administrator\AppData\Local\Temp\nap-b1-pydeps",
            ]
        )
        code = "import json; from source_cleaning_runtime.package_identity import load_identity; print(json.dumps(load_identity(), sort_keys=True))"
        with tempfile.TemporaryDirectory() as directory:
            install_root = Path(directory)
            shutil.copytree(PLUGIN, install_root, dirs_exist_ok=True)
            env["PYTHONPATH"] = str(install_root) + ";" + env["PYTHONPATH"]
            result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
            installed = json.loads(result.stdout)
            self.assertEqual(installed, outer_identity)
            self.assertEqual(installed["release_id"], expected["release_id"])

        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(missing_root)
            env["PYTHONPATH"] = str(missing_root) + ";" + ";".join([str(ROOT / "sdk"), r"C:\Users\Administrator\AppData\Local\Temp\nap-b1-pydeps"])
            missing = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("identity artifact is unavailable", missing.stderr)

        with tempfile.TemporaryDirectory() as directory:
            missing_manifest_root = Path(directory)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(missing_manifest_root)
            shutil.copyfile(sidecar, missing_manifest_root / expected["wheel_import"] / "identity.json")
            env["PYTHONPATH"] = str(missing_manifest_root) + ";" + ";".join([str(ROOT / "sdk"), r"C:\Users\Administrator\AppData\Local\Temp\nap-b1-pydeps"])
            missing_manifest = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
            self.assertNotEqual(missing_manifest.returncode, 0)
            self.assertIn("package manifest is unavailable", missing_manifest.stderr)

    def test_production_uses_frozen_rpc_host_reads_complete_asset_without_request_text(self) -> None:
        case = _case()
        host = FrozenRpcHost(case["source_text"])
        request = _request(case)
        self.assertNotIn("source_text", request)
        result = preview(request, host)
        self.assertEqual(result["contract_id"], "artifact-bundle/v1")
        methods = [method for method, _ in host.calls]
        self.assertIn("host.asset.read/v1", methods)
        self.assertIn("host.asset.create/v1", methods)
        self.assertIn("host.asset.upload.status/v1", methods)
        self.assertNotIn("host.asset.create", methods)
        read_calls = [params for method, params in host.calls if method == "host.asset.read/v1"]
        self.assertGreater(len(read_calls), 1)
        self.assertTrue(all(set(params) == {"asset_id", "offset", "length"} for params in read_calls))
        payload_asset = result["items"][0]["payload_asset_id"]
        payload = json.loads(host.assets[payload_asset].decode("utf-8"))
        self.assertEqual(payload["source_text_hash"], sha256_text(case["source_text"]))
        self.assertEqual(payload["cleaned_text"], case["expected_cleaned_text"])

    def test_request_rejects_raw_source_input_bypasses_for_preview_and_apply(self) -> None:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), preview_host)
        preview_payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        forbidden = ("source_text", "raw", "path", "url", "clipboard")
        for field in forbidden:
            with self.subTest(capability=CAPABILITY_PREVIEW, field=field):
                request = _request(case)
                request[field] = "caller-controlled"
                with self.assertRaisesRegex(ContractError, "UNKNOWN_FIELD"):
                    preview(request, FrozenRpcHost(case["source_text"]))
            with self.subTest(capability=CAPABILITY_APPLY, field=field):
                request = _request(case, capability=CAPABILITY_APPLY, review_context=build_review_context(preview_payload))
                request[field] = "caller-controlled"
                with self.assertRaisesRegex(ContractError, "UNKNOWN_FIELD"):
                    apply(request, FrozenRpcHost(case["source_text"]))

    def test_source_asset_hash_mismatch_fails_closed_after_complete_read(self) -> None:
        case = _case()
        host = FrozenRpcHost(case["source_text"] + "tampered")
        with self.assertRaisesRegex(ContractError, "HASH_BINDING_INVALID|ASSET_READ_ERROR"):
            preview(_request(case), host)

    def test_public_sdk_verifiers_are_required_and_invoked(self) -> None:
        case = _case()
        host = FrozenRpcHost(case["source_text"])
        calls: list[str] = []
        original_result = runtime._sdk_verify_result
        original_receipt = runtime._sdk_verify_receipt
        original_checkpoint = runtime._sdk_verify_checkpoint

        def result_spy(*args, **kwargs):
            calls.append("result")
            return original_result(*args, **kwargs)

        def receipt_spy(*args, **kwargs):
            calls.append("provenance")
            return original_receipt(*args, **kwargs)

        def checkpoint_spy(*args, **kwargs):
            calls.append("checkpoint")
            return original_checkpoint(*args, **kwargs)

        with patch.object(runtime, "_sdk_verify_result", result_spy), patch.object(runtime, "_sdk_verify_receipt", receipt_spy), patch.object(runtime, "_sdk_verify_checkpoint", checkpoint_spy):
            preview(_request(case), host)
        self.assertIn("result", calls)
        self.assertIn("provenance", calls)
        self.assertIn("checkpoint", calls)

    def test_sdk_unavailable_is_fail_closed(self) -> None:
        case = _case()
        with patch.object(runtime, "_SDK_IMPORT_ERROR", RuntimeError("test SDK unavailable")):
            with self.assertRaisesRegex(ContractError, "SDK_UNAVAILABLE"):
                preview(_request(case), FrozenRpcHost(case["source_text"]))

    def test_catalog_lifecycle_event_checkpoint_complete_and_candidate_stage_only_on_apply(self) -> None:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_request = _request(case)
        preview_result = preview(preview_request, preview_host)
        self.assertFalse(preview_host.stage_calls)
        self.assertTrue(any(method == "host.job.event/v1" for method, _ in preview_host.calls))
        self.assertTrue(any(method == "host.checkpoint.commit/v1" for method, _ in preview_host.calls))
        self.assertEqual(preview_host.completions[-1]["outcome"], "succeeded")

        preview_payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        apply_host = FrozenRpcHost(case["source_text"])
        apply_request = _request(case, capability=CAPABILITY_APPLY, review_context=build_review_context(preview_payload))
        applied = apply(apply_request, apply_host)
        self.assertEqual(applied["contract_id"], "candidate-batch/v1")
        self.assertEqual(len(apply_host.stage_calls), 1)
        self.assertEqual(set(apply_host.stage_calls[0]), {"operation_key", "result_bundle_asset_id", "input_snapshot_hash"})
        self.assertIsNotNone(apply_host.completions[-1]["candidate_stage_operation_key"])

        checkpoint_call = next(
            params for method, params in apply_host.calls if method == "host.checkpoint.commit/v1"
        )
        checkpoint_asset_id = str(checkpoint_call["checkpoint_asset_id"])
        checkpoint_raw = apply_host.assets[checkpoint_asset_id]
        checkpoint = json.loads(checkpoint_raw.decode("utf-8"))
        state_asset_id = str(checkpoint["state_asset_id"])
        state_raw = apply_host.assets[state_asset_id]
        resumed = copy.deepcopy(apply_request)
        resumed["operation"] = "resume"
        resumed.update(
            {
                "resume_checkpoint_asset_id": checkpoint_asset_id,
                "resume_checkpoint_asset_hash": _sha(checkpoint_raw),
                "resume_state_asset_id": state_asset_id,
                "resume_state_asset_hash": _sha(state_raw),
            }
        )
        resumed_result = apply(resumed, apply_host, engine=type("NoRecompute", (), {"compile": lambda self, *args, **kwargs: (_ for _ in ()).throw(AssertionError("resume recomputed cleaning"))})())
        self.assertEqual(resumed_result["contract_id"], "candidate-batch/v1")
        self.assertEqual(canonical_bytes(resumed_result), canonical_bytes(applied))
        self.assertNotEqual(apply_host.completions[-1]["outcome"], "failed")

        merge_host = FrozenRpcHost(case["source_text"])
        merge_request = make_merge_request(
            job_id="job-merge-preaudit",
            step_id="step-merge",
            attempt_id="attempt-merge-preaudit",
            lease_epoch=1,
            run_snapshot_hash=sha256_text("merge-preaudit"),
            workspace_id="workspace-nap01",
            rules_packages=[
                {"package_id": "webnovel-ads", "version": "1.0.0", "release_id": _expected()["release_id"], "package_hash": _expected()["package_hash"], "rules": _rules()}
            ],
        )
        merge_request.update({"worker_run_id": "worker-merge-preaudit", "checkpoint_ids": ["checkpoint-merge-1"], "provenance_receipt_id": "receipt-preaudit", "created_at": "2026-08-27T00:00:00Z", "total_units": 1})
        merged = merge_rules(merge_request, merge_host)
        self.assertEqual(merged["contract_id"], "artifact-bundle/v1")
        self.assertFalse(merge_host.stage_calls)
        merge_request["operation"] = "validate"
        validated = merge_rules(merge_request, merge_host)
        self.assertEqual(validated["contract_id"], "artifact-bundle/v1")

        cancel_host = FrozenRpcHost(case["source_text"])
        cancelled = runtime.dispatch({**_request(case), "operation": "cancel"}, cancel_host)
        self.assertIsNone(cancelled)
        self.assertEqual(cancel_host.completions[-1]["outcome"], "cancelled")
        self.assertFalse(cancel_host.stage_calls)

    def test_resume_checkpoint_and_state_asset_tampering_fails_closed(self) -> None:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), preview_host)
        preview_payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        apply_request = _request(case, capability=CAPABILITY_APPLY, review_context=build_review_context(preview_payload))
        for tamper in ("checkpoint", "state"):
            with self.subTest(tamper=tamper):
                host = FrozenRpcHost(case["source_text"])
                applied = apply(apply_request, host)
                checkpoint_params = next(
                    params for method, params in host.calls if method == "host.checkpoint.commit/v1"
                )
                checkpoint_id = str(checkpoint_params["checkpoint_asset_id"])
                checkpoint = json.loads(host.assets[checkpoint_id].decode("utf-8"))
                state_id = str(checkpoint["state_asset_id"])
                request = copy.deepcopy(apply_request)
                request.update(
                    {
                        "operation": "resume",
                        "resume_checkpoint_asset_id": checkpoint_id,
                        "resume_checkpoint_asset_hash": _sha(host.assets[checkpoint_id]),
                        "resume_state_asset_id": state_id,
                        "resume_state_asset_hash": _sha(host.assets[state_id]),
                    }
                )
                target_id = checkpoint_id if tamper == "checkpoint" else state_id
                host.assets[target_id] += b"\n tampered"
                with self.assertRaises(ContractError):
                    apply(request, host)
                self.assertEqual(host.completions[-1]["outcome"], "failed")

    def test_candidate_stage_objects_project_to_non_empty_provenance_item_ids(self) -> None:
        case = _case()
        host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), host)
        preview_payload = json.loads(host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        request = _request(
            case,
            capability=CAPABILITY_APPLY,
            review_context=build_review_context(preview_payload),
        )
        receipts: list[dict[str, object]] = []
        rpc_methods: list[str] = []
        original_rpc_validator = runtime._sdk_validate_rpc_result
        original_receipt = runtime._provenance_receipt

        def rpc_validator_spy(method, result):
            rpc_methods.append(method)
            return original_rpc_validator(method, result)

        def receipt_spy(*args, **kwargs):
            receipt = original_receipt(*args, **kwargs)
            receipts.append(copy.deepcopy(receipt))
            return receipt

        with patch.object(runtime, "_sdk_validate_rpc_result", side_effect=rpc_validator_spy), patch.object(runtime, "_provenance_receipt", side_effect=receipt_spy):
            result = apply(request, host)

        self.assertEqual(result["contract_id"], "candidate-batch/v1")
        self.assertIn("host.candidate.stage/v1", rpc_methods)
        expected_item_id = result["items"][0]["item_id"]
        self.assertEqual(
            host.stage_responses[-1]["staged_items"],
            [{
                "item_id": expected_item_id,
                "candidate_id": f"candidate-{expected_item_id[:16]}",
                "stage_status": "created",
                "publication_eligibility": "review_only",
            }],
        )
        self.assertEqual(receipts[-1]["staged_items"], [expected_item_id])
        self.assertTrue(all(isinstance(item_id, str) and item_id for item_id in receipts[-1]["staged_items"]))
        verify_provenance_receipt(receipts[-1])

    def test_candidate_stage_rejects_empty_missing_duplicate_mismatched_and_failed_rows(self) -> None:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), preview_host)
        preview_payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        review = build_review_context(preview_payload)
        for mode in ("empty", "missing", "duplicate", "mismatch", "failed"):
            with self.subTest(stage_mode=mode):
                host = FrozenRpcHost(case["source_text"], stage_mode=mode)
                with self.assertRaises(ContractError):
                    apply(_request(case, capability=CAPABILITY_APPLY, review_context=review), host)
                self.assertTrue(host.stage_calls)
                self.assertEqual(host.completions[-1]["outcome"], "failed")

    def test_apply_cancellation_checkpoint_commit_failure_is_terminal_failure(self) -> None:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), preview_host)
        preview_payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        host = FrozenRpcHost(case["source_text"], fail_checkpoint_commit=True)
        with self.assertRaisesRegex(ContractError, "authoritative checkpoint/state"):
            runtime.dispatch(
                {
                    **_request(case, capability=CAPABILITY_APPLY, review_context=build_review_context(preview_payload)),
                    "operation": "cancel",
                },
                host,
            )
        self.assertEqual(host.completions[-1]["outcome"], "failed")
        self.assertFalse(host.stage_calls)

    def test_declared_entrypoint_uses_accepted_request_host_loader_signature(self) -> None:
        case = _case()
        plugin_manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_manifest["backend"]["entrypoint"], "source_cleaning_runtime.runtime:main")
        advertised = runtime.main()
        verify_capability_descriptor(advertised, expected_capability_id=CAPABILITY_PREVIEW)
        host = FrozenRpcHost(case["source_text"])
        result = runtime.main(_request(case), host)
        self.assertEqual(result["contract_id"], "artifact-bundle/v1")

    def test_descriptors_bind_expected_sha_provider_and_complete_catalog_fields(self) -> None:
        expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "catalog/plugin-catalog-v1.json").read_text(encoding="utf-8"))
        entry = next(item for item in catalog["code_plugins"] if item["plugin_id"] == expected["plugin_id"])
        names = {
            "source.clean.preview/v1": "descriptor.json",
            "source.clean.apply/v1": "descriptor-apply.json",
            "source.clean.rules.merge/v1": "descriptor-merge.json",
        }
        sha_fields = {
            "descriptor.json": "descriptor_sha256",
            "descriptor-apply.json": "descriptor_apply_sha256",
            "descriptor-merge.json": "descriptor_merge_sha256",
        }
        index_paths = expected["schema_index_paths"]
        for capability_id, name in names.items():
            path = PLUGIN / name
            descriptor = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(_sha(path.read_bytes()), expected[sha_fields[name]])
            catalog_capability = next(item for item in entry["capabilities"] if item["capability_id"] == capability_id)
            verify_capability_descriptor(
                descriptor,
                expected_capability_id=capability_id,
                expected_provider={"plugin_id": expected["plugin_id"], "release_id": expected["release_id"]},
            )
            for descriptor_field, catalog_field in (
                ("input_schema", "input_schema"),
                ("output_schema", "output_schema"),
                ("result_contract", "result_contract"),
                ("supports", "operations"),
                ("deterministic", "deterministic"),
                ("accepted_data_formats", "accepted_data_formats"),
            ):
                self.assertEqual(descriptor[descriptor_field], catalog_capability[catalog_field])
            index = json.loads((PLUGIN / index_paths[capability_id]).read_text(encoding="utf-8"))
            self.assertEqual(set(index), {"schema", "plugin_id", "capability_id", "schemas"})
            self.assertEqual(index["schema"], "provider-schema-index/v1")
            self.assertEqual(index["plugin_id"], expected["plugin_id"])
            self.assertEqual(index["capability_id"], capability_id)
            self.assertEqual(
                [(item["schema_id"], item["path"]) for item in index["schemas"]],
                [(descriptor["input_schema"], "schemas/" + Path(expected["input_schema_paths"][list(names).index(capability_id)]).name),
                 (descriptor["output_schema"], "schemas/" + Path(expected["output_schema_paths"][list(names).index(capability_id)]).name)],
            )
            for item in index["schemas"]:
                schema_path = PLUGIN / "source_cleaning_runtime" / item["path"]
                self.assertTrue(schema_path.is_file())
                self.assertEqual(_sha(schema_path.read_bytes()), item["sha256"])
                schema_artifact = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertEqual(schema_artifact["$id"], item["schema_id"])
                self.assertEqual(item["path"].split("/", 1)[0], "schemas")
        lock = (PLUGIN / "backend/requirements.lock").read_text(encoding="utf-8")
        self.assertIn("regex==2026.7.19", lock)
        self.assertIn("public PlotPilot SDK", (PLUGIN / "backend/wheels/README.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
