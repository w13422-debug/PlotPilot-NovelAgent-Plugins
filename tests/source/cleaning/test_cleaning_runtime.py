from __future__ import annotations

import copy
import ast
import base64
import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "source-cleaning-runtime"))

import source_cleaning_runtime.runtime as runtime  # noqa: E402
from source_cleaning_runtime import (  # noqa: E402
    CAPABILITY_APPLY,
    CAPABILITY_PREVIEW,
    ContractError,
    RegexRuntimeUnavailable,
    build_profile,
    build_review_context,
    make_cleaning_request,
    make_merge_request,
    merge_rules,
    preview,
    read_asset_json,
    regex_runtime_status,
    rules_hash,
    sha256_text,
    validate_rules,
    apply,
)


class MemoryAssetHost:
    """Frozen HostPort.call double for Asset, lifecycle, checkpoint, and stage RPCs."""

    def __init__(self, source_assets: dict[str, bytes] | None = None, *, page_size: int = 4, fail_checkpoint_commit: bool = False) -> None:
        self.assets: dict[str, bytes] = dict(source_assets or {})
        self.page_size = page_size
        self.fail_checkpoint_commit = fail_checkpoint_commit
        self.uploads: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.create_calls = 0
        self.event_seq = 0
        self.stage_calls: list[dict[str, object]] = []
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
            chunk = asset[offset : offset + min(int(params["length"]), self.page_size)]
            end = offset + len(chunk)
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": None if end == len(asset) else end,
                "content_hash": hashlib.sha256(chunk).hexdigest(),
            }
        if method == "host.asset.create/v1":
            chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
            if int(params["offset"]) != 0 or params["final"] is not True:
                raise AssertionError("cleaning must use one final Asset upload")
            if len(chunk) != int(params["total_size"]):
                raise AssertionError("upload size mismatch")
            if hashlib.sha256(chunk).hexdigest() != params["chunk_hash"] or params["expected_hash"] != params["chunk_hash"]:
                raise AssertionError("upload hash mismatch")
            self.create_calls += 1
            asset_id = f"test-asset-{str(params['expected_hash'])[:32]}"
            self.assets[asset_id] = chunk
            self.uploads[str(params["upload_id"])] = asset_id
            return {"upload_id": params["upload_id"], "accepted_bytes": len(chunk), "completed": True, "asset_id": asset_id}
        if method == "host.asset.upload.status/v1":
            asset_id = self.uploads[str(params["upload_id"])]
            data = self.assets[asset_id]
            if params["expected_hash"] != hashlib.sha256(data).hexdigest():
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
            return {"accepted": True, "checkpoint_id": checkpoint["checkpoint_id"], "completed_units": checkpoint["completed_units"], "total_units": checkpoint["total_units"], "job_event_seq": self.event_seq}
        if method == "host.candidate.stage/v1":
            self.stage_calls.append(dict(params))
            bundle = json.loads(self.assets[str(params["result_bundle_asset_id"])].decode("utf-8"))
            self.event_seq += 1
            return {
                "accepted": True,
                "staged_items": [
                    {
                        "item_id": item["item_id"],
                        "candidate_id": f"candidate-{item['item_id'][:16]}",
                        "stage_status": "created",
                        "publication_eligibility": "review_only",
                    }
                    for item in bundle["items"]
                ],
                "job_event_seq": self.event_seq,
            }
        if method == "host.job.complete/v1":
            self.completions.append(dict(params))
            self.event_seq += 1
            outcome = str(params["outcome"])
            worker = str(params["worker_run_id"])
            receipt = "receipt-" + worker.removeprefix("worker-")
            return {"accepted": True, "attempt_state": outcome, "step_state": outcome, "job_state": outcome, "provenance_receipt_id": receipt, "job_event_seq": self.event_seq, "core_event_high_water": self.event_seq}
        raise AssertionError(f"unexpected RPC: {method}")


def _rules() -> dict:
    return json.loads(
        (ROOT / "data" / "source-cleaning" / "rules" / "data" / "rules.json").read_text(encoding="utf-8")
    )


def _expected() -> dict:
    return json.loads(
        (ROOT / "data" / "source-cleaning" / "rules" / "expected.json").read_text(encoding="utf-8")
    )


def _golden() -> list[dict]:
    return json.loads(
        (ROOT / "data" / "source-cleaning" / "fixtures" / "cleaning" / "golden_cases.json").read_text(encoding="utf-8")
    )["cases"]


def _request(case: dict, *, capability: str = CAPABILITY_PREVIEW, review_context: dict | None = None, snapshot: str | None = None, limits: dict | None = None) -> dict:
    expected = _expected()
    text = case["source_text"]
    return make_cleaning_request(
        capability=capability,
        job_id=f"job-{case['id']}",
        step_id="step-clean",
        attempt_id=f"attempt-{case['id']}",
        lease_epoch=1,
        run_snapshot_hash=snapshot or sha256_text("run-snapshot-v1"),
        workspace_id="workspace-nap01",
        document_id=f"document-{case['id']}",
        revision_id=f"revision-{case['id']}",
        base_content_hash=sha256_text(text),
        source_asset_id=f"source-{case['id']}",
        source_text=text,
        rules=_rules(),
        rules_release_id=expected["release_id"],
        rules_package_hash=expected["package_hash"],
        input_kind=case["input_kind"],
        storage_source_format=case["storage_source_format"],
        review_context=review_context,
        limits=limits,
    )


class CleaningRuntimeTests(unittest.TestCase):
    def test_cleaning_runtime_has_no_forbidden_regex_or_thread_fallback_import(self) -> None:
        package_root = ROOT / "plugins" / "source-cleaning-runtime" / "source_cleaning_runtime"
        for path in package_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn(node.names[0].name, {"re", "threading"}, str(path))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"re", "threading"}, str(path))

    def test_exact_regex_lock_is_available_only_from_isolated_dependency(self) -> None:
        status = regex_runtime_status()
        self.assertTrue(status["available"], status)
        self.assertEqual(status["engine"], "core_regex/v1")
        self.assertEqual(status["package"], "regex")
        self.assertEqual(status["version"], "2026.7.19")
        self.assertEqual(status["syntax_version"], "regex.VERSION1")
        self.assertTrue(status["native_timeout"])

    def test_native_timeout_capability_probe_is_stable_under_repetition(self) -> None:
        for _ in range(12):
            engine = runtime._locked_regex_engine()
            self.assertEqual(engine.engine_id, "core_regex/v1")
            self.assertTrue(engine.native_timeout)

    def test_regex_without_timeout_keyword_fails_closed(self) -> None:
        class NoTimeoutPattern:
            def search(self, value: str) -> None:
                return None

        class NoTimeoutModule:
            __version__ = runtime.CORE_REGEX_VERSION
            VERSION1 = object()

            @staticmethod
            def compile(pattern: str, flags: object) -> NoTimeoutPattern:
                return NoTimeoutPattern()

        with patch.object(runtime.importlib, "import_module", return_value=NoTimeoutModule()), patch.object(
            runtime.importlib.metadata, "version", return_value=runtime.CORE_REGEX_VERSION
        ):
            with self.assertRaisesRegex(RegexRuntimeUnavailable, "native timeout keyword is unavailable"):
                runtime._locked_regex_engine()

    def test_real_rule_native_and_wall_timeouts_remain_bound_to_original_limits(self) -> None:
        case = _golden()[0]
        observed: list[float] = []

        class TimeoutPattern:
            def finditer(self, value: str, *, overlapped: bool, timeout: float):
                observed.append(timeout)
                raise TimeoutError("native timeout")

        class LockedTimeoutEngine:
            engine_id = runtime.CORE_REGEX_ENGINE
            package_name = runtime.CORE_REGEX_PACKAGE
            dependency_version = runtime.CORE_REGEX_VERSION
            syntax_version = runtime.CORE_REGEX_SYNTAX_VERSION
            native_timeout = True

            def compile(self, pattern: str, *, flags: object = 0) -> TimeoutPattern:
                return TimeoutPattern()

            def flags_for(self, names: object) -> int:
                return 0

        with self.assertRaisesRegex(ContractError, "TIMEOUT"):
            preview(_request(case), MemoryAssetHost({f"source-{case['id']}": case["source_text"].encode("utf-8")}), engine=LockedTimeoutEngine())
        self.assertEqual(observed, [0.1])

        class SlowPattern:
            def finditer(self, value: str, *, overlapped: bool, timeout: float):
                time.sleep(0.01)
                return iter(())

        class LockedSlowEngine(LockedTimeoutEngine):
            def compile(self, pattern: str, *, flags: object = 0) -> SlowPattern:
                return SlowPattern()

        limits = dict(runtime.DEFAULT_LIMITS)
        limits["scope_timeout_ms"] = 1
        limits["rule_timeout_ms"] = 1
        with self.assertRaisesRegex(ContractError, "rule timeout exceeded"):
            preview(
                _request(case, limits=limits),
                MemoryAssetHost({f"source-{case['id']}": case["source_text"].encode("utf-8")}),
                engine=LockedSlowEngine(),
            )

    def test_preview_apply_parity_and_candidate_only(self) -> None:
        for case in _golden():
            with self.subTest(case=case["id"]):
                host = MemoryAssetHost({f"source-{case['id']}": case["source_text"].encode("utf-8")})
                preview_result = preview(_request(case), host)
                self.assertEqual(preview_result["contract_id"], "artifact-bundle/v1")
                self.assertEqual(preview_result["bundle_type"], "artifact")
                self.assertEqual(preview_result["items"][0]["schema"], "artifact-item/v1")
                preview_payload = read_asset_json(
                    host,
                    preview_result["items"][0]["payload_asset_id"],
                    preview_result["items"][0]["payload_hash"],
                )
                self.assertEqual(preview_payload["cleaned_text"], case["expected_cleaned_text"])
                self.assertEqual(preview_payload["canonical_text_hash"], sha256_text(case["expected_cleaned_text"]))
                self.assertEqual(preview_payload["source_text_hash"], sha256_text(case["source_text"]))
                for row in preview_payload["match_rows"]:
                    self.assertEqual(
                        case["source_text"][row["start_codepoint"] : row["end_codepoint"]],
                        row["quote"],
                    )

                review = build_review_context(preview_payload)
                apply_result = apply(_request(case, capability=CAPABILITY_APPLY, review_context=review), host)
                self.assertEqual(apply_result["contract_id"], "candidate-batch/v1")
                self.assertEqual(apply_result["bundle_type"], "candidate_batch")
                self.assertEqual(apply_result["items"][0]["schema"], "candidate-item/v1")
                self.assertEqual(apply_result["items"][0]["item_kind"], "document")
                candidate_payload = read_asset_json(
                    host,
                    apply_result["items"][0]["payload_asset_id"],
                    apply_result["items"][0]["mutation"]["payload_hash"],
                )
                self.assertEqual(candidate_payload["schema"], "core/document-text/v1")
                self.assertEqual(candidate_payload["text"], case["expected_cleaned_text"])

                changed_snapshot = sha256_text("different-snapshot")
                with self.assertRaisesRegex(ContractError, "PARITY_MISMATCH"):
                    apply(
                        _request(
                            case,
                            capability=CAPABILITY_APPLY,
                            review_context=review,
                            snapshot=changed_snapshot,
                        ),
                        host,
                    )

                changed_review = copy.deepcopy(review)
                changed_review["match_rows"] = []
                with self.assertRaisesRegex(ContractError, "PARITY_MISMATCH"):
                    apply(_request(case, capability=CAPABILITY_APPLY, review_context=changed_review), host)

    def test_preview_requires_host_asset_port_and_never_fabricates_assets(self) -> None:
        with self.assertRaisesRegex(ContractError, "HOST_ASSET_BINDING_INVALID"):
            preview(_request(_golden()[0]), None)

    def test_rules_merge_is_deterministic_and_artifact_only(self) -> None:
        expected = _expected()
        rules = _rules()
        second = copy.deepcopy(rules)
        second["rules"] = [copy.deepcopy(rules["rules"][1])]
        second["rules"][0]["id"] = "second-package-tail"
        second["rules"][0]["order"] = 10
        packages = [
            {"package_id": "webnovel-ads", "version": "1.0.0", "release_id": expected["release_id"], "package_hash": expected["package_hash"], "rules": rules},
            {"package_id": "tail-package", "version": "1.0.0", "release_id": "1" * 64, "package_hash": "2" * 64, "rules": second},
        ]
        request = make_merge_request(
            job_id="job-merge",
            step_id="step-merge",
            attempt_id="attempt-merge",
            lease_epoch=1,
            run_snapshot_hash=sha256_text("merge-snapshot"),
            workspace_id="workspace-nap01",
            rules_packages=packages,
        )
        first = merge_rules(request, MemoryAssetHost())
        second_result = merge_rules(request, MemoryAssetHost())
        self.assertEqual(first, second_result)
        self.assertEqual(first["contract_id"], "artifact-bundle/v1")
        self.assertEqual(first["items"][0]["schema"], "artifact-item/v1")
        self.assertNotIn("candidate-item/v1", {item["schema"] for item in first["items"]})

        duplicate = copy.deepcopy(packages)
        duplicate[1]["rules"]["rules"][0]["id"] = rules["rules"][0]["id"]
        with self.assertRaisesRegex(ContractError, "RULE_ID_DUPLICATE"):
            merge_rules(
                make_merge_request(
                    job_id="job-merge-duplicate",
                    step_id="step-merge",
                    attempt_id="attempt-merge",
                    lease_epoch=1,
                    run_snapshot_hash=sha256_text("merge-snapshot"),
                    workspace_id="workspace-nap01",
                    rules_packages=duplicate,
                ),
                MemoryAssetHost(),
            )

    def test_rules_closed_shape_and_order_identity(self) -> None:
        rules = _rules()
        self.assertEqual([rule["order"] for rule in rules["rules"]], [10, 20])
        self.assertEqual(rules_hash(rules), rules_hash({"schema": rules["schema"], "rules": list(reversed(rules["rules"]))}))
        with self.assertRaisesRegex(ContractError, "UNKNOWN_FIELD"):
            validate_rules({"schema": rules["schema"], "rules": [{**rules["rules"][0], "replacement": ""}, rules["rules"][1]]})
        with self.assertRaisesRegex(ContractError, "RULE_ORDER_DUPLICATE"):
            validate_rules({"schema": rules["schema"], "rules": [{**rules["rules"][0], "order": 20}, rules["rules"][1]]})
        with self.assertRaisesRegex(ContractError, "flags must use frozen order"):
            validate_rules({"schema": rules["schema"], "rules": [{**rules["rules"][0], "flags": ["multiline", "ignore_case"]}, rules["rules"][1]]})

    def test_profile_hash_binds_rule_release_and_exclusions(self) -> None:
        rules = _rules()
        profile = build_profile(
            rules,
            rules_release_id="a" * 64,
            rules_package_hash="b" * 64,
            excluded_hit_ids=[],
        )
        self.assertEqual(profile["schema"], "source-cleaning-profile/v1")
        self.assertEqual(profile["rule_order"], ["remove-site-ad-line", "remove-next-page-line"])
        with self.assertRaisesRegex(ContractError, "HASH_BINDING_INVALID"):
            from source_cleaning_runtime import validate_profile

            validate_profile({**profile, "rules_hash": "c" * 64})


if __name__ == "__main__":
    unittest.main()
