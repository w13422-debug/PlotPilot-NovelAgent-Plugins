from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "source-cleaning-runtime"
sys.path[:0] = [str(PLUGIN), str(ROOT / "sdk"), str(Path(__file__).parent)]

import source_cleaning_runtime.runtime as runtime  # noqa: E402
from source_cleaning_runtime import (  # noqa: E402
    CAPABILITY_APPLY,
    CAPABILITY_PREVIEW,
    ContractError,
    SourceCleaningRuntime,
    apply,
    build_review_context,
    preview,
)
from source_cleaning_runtime.package_identity import load_identity  # noqa: E402
from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs  # noqa: E402

from test_controller_preaudit import FrozenRpcHost, _case, _request, _sha  # noqa: E402


def _request_for_text(
    text: str,
    *,
    capability: str = CAPABILITY_PREVIEW,
    review_context: dict | None = None,
    max_snapshot_bytes: int,
) -> dict:
    case = copy.deepcopy(_case())
    case["source_text"] = text
    request = _request(case, capability=capability, review_context=review_context)
    request["limits"]["max_snapshot_bytes"] = max_snapshot_bytes
    return request


def _resume_request(request: dict, host: FrozenRpcHost) -> dict:
    checkpoint_params = [
        params for method, params in host.calls if method == "host.checkpoint.commit/v1"
    ][-1]
    checkpoint_asset_id = str(checkpoint_params["checkpoint_asset_id"])
    checkpoint_raw = host.assets[checkpoint_asset_id]
    checkpoint = json.loads(checkpoint_raw.decode("utf-8"))
    state_asset_id = str(checkpoint["state_asset_id"])
    resumed = copy.deepcopy(request)
    resumed["operation"] = "resume"
    resumed.update(
        {
            "resume_checkpoint_asset_id": checkpoint_asset_id,
            "resume_checkpoint_asset_hash": _sha(checkpoint_raw),
            "resume_state_asset_id": state_asset_id,
            "resume_state_asset_hash": _sha(host.assets[state_asset_id]),
        }
    )
    return resumed


class PackageIdentityBindingTests(unittest.TestCase):
    def _copy_package(self, root: Path) -> Path:
        shutil.copytree(PLUGIN, root, dirs_exist_ok=True)
        return root

    def test_outer_manifest_payload_and_identity_tampering_fail_closed(self) -> None:
        expected = json.loads((PLUGIN / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(load_identity(), {
            "schema": "source-plugin-package-identity/v1",
            "plugin_id": expected["plugin_id"],
            "version": expected["version"],
            "package_hash": expected["package_hash"],
            "release_id": expected["release_id"],
        })

        cases = ("identity_missing", "manifest_missing", "payload_missing", "payload_tamper", "manifest_tamper", "package_wrong", "release_wrong")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = self._copy_package(Path(directory))
                identity_path = root / "source_cleaning_runtime" / "identity.json"
                manifest_path = root / "files.sha256"
                if case == "identity_missing":
                    identity_path.unlink()
                elif case == "manifest_missing":
                    manifest_path.unlink()
                elif case == "payload_missing":
                    (root / "source_cleaning_runtime" / "runtime.py").unlink()
                elif case == "payload_tamper":
                    with (root / "source_cleaning_runtime" / "runtime.py").open("ab") as stream:
                        stream.write(b"\n# tampered\n")
                elif case == "manifest_tamper":
                    rows = manifest_path.read_text(encoding="utf-8").splitlines()
                    rows[0] = ("0" * 64) + rows[0][64:]
                    manifest_path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
                else:
                    identity = json.loads(identity_path.read_text(encoding="utf-8"))
                    identity["package_hash" if case == "package_wrong" else "release_id"] = "0" * 64
                    identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8", newline="\n")
                with self.assertRaises(RuntimeError):
                    load_identity(root)

    def test_provenance_uses_one_verified_outer_identity(self) -> None:
        case = _case()
        host = FrozenRpcHost(case["source_text"])
        receipts: list[dict] = []
        original = runtime._provenance_receipt

        def receipt_spy(*args, **kwargs):
            receipt = original(*args, **kwargs)
            receipts.append(copy.deepcopy(receipt))
            return receipt

        with patch.object(runtime, "load_identity", wraps=load_identity) as verified, patch.object(
            runtime, "_provenance_receipt", side_effect=receipt_spy
        ):
            preview(_request(case), host)
        identity = load_identity()
        self.assertGreaterEqual(verified.call_count, 1)
        self.assertEqual(receipts[-1]["package_hash"], identity["package_hash"])
        self.assertEqual(receipts[-1]["release_id"], identity["release_id"])


class _BlockingCompiled:
    def __init__(self, entered: Event, released: Event) -> None:
        self.entered = entered
        self.released = released

    def finditer(self, *_args, **_kwargs):
        self.entered.set()
        if not self.released.wait(5):
            raise AssertionError("test did not release blocked rule loop")
        return iter((type("Match", (), {"start": lambda self: 0, "end": lambda self: 1})(),))


class _BlockingEngine:
    engine_id = runtime.CORE_REGEX_ENGINE
    package_name = runtime.CORE_REGEX_PACKAGE
    dependency_version = runtime.CORE_REGEX_VERSION
    syntax_version = runtime.CORE_REGEX_SYNTAX_VERSION
    native_timeout = True

    def __init__(self, entered: Event, released: Event) -> None:
        self.compiled = _BlockingCompiled(entered, released)

    def compile(self, *_args, **_kwargs):
        return self.compiled

    def flags_for(self, _flags):
        return 0


class _CompletionBarrierHost(FrozenRpcHost):
    def __init__(self, source_text: str) -> None:
        super().__init__(source_text)
        self.success_entered = Event()
        self.success_released = Event()

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "host.job.complete/v1" and params.get("outcome") == "succeeded":
            self.success_entered.set()
            if not self.success_released.wait(5):
                raise AssertionError("test did not release success completion")
        return super().call(method, params)


class ActiveWorkerCancellationTests(unittest.TestCase):
    def _apply_request(self) -> dict:
        case = _case()
        preview_host = FrozenRpcHost(case["source_text"])
        preview_result = preview(_request(case), preview_host)
        payload = json.loads(preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        request = _request(case, capability=CAPABILITY_APPLY, review_context=build_review_context(payload))
        request["checkpoint_ids"] = ["checkpoint-preaudit-1", "checkpoint-preaudit-2"]
        request["total_units"] = 2
        return request

    def test_cancel_before_worker_registration_reserves_terminal_owner_atomically(self) -> None:
        case = _case()
        request = self._apply_request()
        cancel_request = {**copy.deepcopy(request), "operation": "cancel"}
        host = FrozenRpcHost(case["source_text"])
        worker = SourceCleaningRuntime()
        reserved, released = Event(), Event()
        terminal_calls: list[str] = []
        cancel_results: list[object] = []
        run_results: list[object] = []
        original_cancel_terminal = runtime._cancel_terminal

        def delayed_cancel_terminal(*args, **kwargs):
            terminal_calls.append(str(args[1]["worker_run_id"]))
            reserved.set()
            if not released.wait(5):
                raise AssertionError("test did not release reserved cancel terminal")
            return original_cancel_terminal(*args, **kwargs)

        def cancel_worker() -> None:
            try:
                cancel_results.append(worker.run(cancel_request, host))
            except BaseException as exc:  # pragma: no cover - asserted below
                cancel_results.append(exc)

        def register_worker() -> None:
            try:
                run_results.append(worker.run(request, host))
            except BaseException as exc:
                run_results.append(exc)

        with patch.object(runtime, "_cancel_terminal", side_effect=delayed_cancel_terminal):
            cancel_thread = Thread(target=cancel_worker)
            cancel_thread.start()
            self.assertTrue(reserved.wait(5))
            run_thread = Thread(target=register_worker)
            run_thread.start()
            run_thread.join(5)
            self.assertFalse(run_thread.is_alive())
            self.assertEqual(len(run_results), 1)
            self.assertIsInstance(run_results[0], ContractError)
            self.assertEqual(run_results[0].code, "WORKER_RUN_TERMINAL_RESERVED")
            released.set()
            cancel_thread.join(5)
            self.assertFalse(cancel_thread.is_alive())

        self.assertEqual(cancel_results, [None])
        self.assertEqual(terminal_calls, [request["worker_run_id"]])
        self.assertEqual([item["outcome"] for item in host.completions], ["cancelled"])
        self.assertEqual(len([1 for method, _ in host.calls if method == "host.checkpoint.commit/v1"]), 1)
        later_host = FrozenRpcHost(case["source_text"])
        self.assertEqual(worker.run(request, later_host)["contract_id"], "candidate-batch/v1")
        self.assertEqual([item["outcome"] for item in later_host.completions], ["succeeded"])

        resumed = _resume_request(request, host)
        self.assertEqual(worker.run(resumed, host)["contract_id"], "candidate-batch/v1")
        self.assertEqual([item["outcome"] for item in host.completions], ["cancelled", "succeeded"])

    def test_two_concurrent_cancel_requests_share_one_terminal_reservation(self) -> None:
        case = _case()
        request = self._apply_request()
        cancel_request = {**copy.deepcopy(request), "operation": "cancel"}
        host = FrozenRpcHost(case["source_text"])
        worker = SourceCleaningRuntime()
        reserved, released = Event(), Event()
        terminal_calls: list[str] = []
        results: list[object] = []
        original_cancel_terminal = runtime._cancel_terminal

        def delayed_cancel_terminal(*args, **kwargs):
            terminal_calls.append(str(args[1]["worker_run_id"]))
            reserved.set()
            if not released.wait(5):
                raise AssertionError("test did not release first cancel terminal")
            return original_cancel_terminal(*args, **kwargs)

        def cancel_worker() -> None:
            try:
                results.append(worker.run(copy.deepcopy(cancel_request), host))
            except BaseException as exc:  # pragma: no cover - asserted below
                results.append(exc)

        with patch.object(runtime, "_cancel_terminal", side_effect=delayed_cancel_terminal):
            first = Thread(target=cancel_worker)
            first.start()
            self.assertTrue(reserved.wait(5))
            second = Thread(target=cancel_worker)
            second.start()
            second.join(5)
            self.assertFalse(second.is_alive())
            self.assertEqual(results, [None])
            self.assertEqual(len(terminal_calls), 1)
            released.set()
            first.join(5)
            self.assertFalse(first.is_alive())

        self.assertEqual(results, [None, None])
        self.assertEqual(terminal_calls, [request["worker_run_id"]])
        self.assertEqual([item["outcome"] for item in host.completions], ["cancelled"])
        self.assertEqual(len([1 for method, _ in host.calls if method == "host.checkpoint.commit/v1"]), 1)

        next_host = FrozenRpcHost(case["source_text"])
        next_request = _request(case)
        next_request["worker_run_id"] = request["worker_run_id"]
        self.assertEqual(worker.run(next_request, next_host)["contract_id"], "artifact-bundle/v1")
        self.assertEqual(next_host.completions[-1]["outcome"], "succeeded")

    def test_concurrent_cancel_has_one_checkpoint_one_terminal_and_resumes_without_signal_leakage(self) -> None:
        case = _case()
        request = self._apply_request()
        host = FrozenRpcHost(case["source_text"])
        worker = SourceCleaningRuntime()
        entered, released = Event(), Event()
        outcomes: list[object] = []

        def run_worker() -> None:
            try:
                outcomes.append(worker.run(request, host, engine=_BlockingEngine(entered, released)))
            except BaseException as exc:  # pragma: no cover - asserted below
                outcomes.append(exc)

        thread = Thread(target=run_worker)
        thread.start()
        self.assertTrue(entered.wait(5))
        cancel_request = {**copy.deepcopy(request), "operation": "cancel"}
        self.assertIsNone(worker.run(cancel_request, host))
        self.assertIsNone(worker.run(cancel_request, host))
        released.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [None])
        cancellation_checkpoints = [params for method, params in host.calls if method == "host.checkpoint.commit/v1"]
        self.assertEqual(len(cancellation_checkpoints), 1)
        self.assertEqual([item["outcome"] for item in host.completions], ["cancelled"])

        resumed = _resume_request(request, host)
        result = worker.run(resumed, host)
        self.assertEqual(result["contract_id"], "candidate-batch/v1")
        self.assertEqual([item["outcome"] for item in host.completions], ["cancelled", "succeeded"])
        self.assertEqual(len([1 for method, _ in host.calls if method == "host.checkpoint.commit/v1"]), 2)

    def test_success_cancel_race_has_one_success_terminal_and_signal_does_not_leak(self) -> None:
        case = _case()
        request = self._apply_request()
        request["checkpoint_ids"] = ["checkpoint-preaudit-1"]
        request["total_units"] = 1
        host = _CompletionBarrierHost(case["source_text"])
        worker = SourceCleaningRuntime()
        outcomes: list[object] = []

        thread = Thread(target=lambda: outcomes.append(worker.run(request, host)))
        thread.start()
        self.assertTrue(host.success_entered.wait(5))
        self.assertIsNone(worker.run({**copy.deepcopy(request), "operation": "cancel"}, host))
        host.success_released.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["contract_id"], "candidate-batch/v1")
        self.assertEqual([item["outcome"] for item in host.completions], ["succeeded"])

        preview_host = FrozenRpcHost(case["source_text"])
        later = _request(case)
        later["worker_run_id"] = request["worker_run_id"]
        self.assertEqual(worker.run(later, preview_host)["contract_id"], "artifact-bundle/v1")
        self.assertEqual(preview_host.completions[-1]["outcome"], "succeeded")

    def test_cancelled_resume_state_binds_run_attempt_lease_snapshot_profile_and_candidate(self) -> None:
        case = _case()
        request = self._apply_request()
        host = FrozenRpcHost(case["source_text"])
        context = runtime._context_for_request(request, capability=CAPABILITY_APPLY)
        runtime._cancel_terminal(host, request, context, capability=CAPABILITY_APPLY)
        resumed = _resume_request(request, host)
        checkpoint_id = resumed["resume_checkpoint_asset_id"]
        checkpoint = json.loads(host.assets[checkpoint_id].decode("utf-8"))
        state_id = checkpoint["state_asset_id"]
        state = json.loads(host.assets[state_id].decode("utf-8"))
        self.assertEqual(state["worker_run_id"], request["worker_run_id"])
        self.assertEqual(state["attempt_id"], request["attempt_id"])
        self.assertEqual(state["lease_epoch"], request["lease_epoch"])
        self.assertEqual(state["run_snapshot_hash"], request["run_snapshot_hash"])
        self.assertEqual(state["profile_hash"], request["profile"]["profile_hash"])
        self.assertIsNone(state["candidate_item"])

        for field, value in (
            ("worker_run_id", "worker-other"),
            ("attempt_id", "attempt-other"),
            ("lease_epoch", 2),
            ("run_snapshot_hash", "0" * 64),
            ("profile.profile_hash", "0" * 64),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(resumed)
                if field == "profile.profile_hash":
                    tampered["profile"]["profile_hash"] = value
                else:
                    tampered[field] = value
                with self.assertRaises(ContractError):
                    apply(tampered, copy.deepcopy(host))

        candidate_host = FrozenRpcHost(case["source_text"])
        apply(request, candidate_host)
        candidate_resume = _resume_request(request, candidate_host)
        candidate_checkpoint_id = candidate_resume["resume_checkpoint_asset_id"]
        candidate_checkpoint = json.loads(candidate_host.assets[candidate_checkpoint_id].decode("utf-8"))
        candidate_state_id = candidate_checkpoint["state_asset_id"]
        candidate_state = json.loads(candidate_host.assets[candidate_state_id].decode("utf-8"))
        candidate_state["candidate_item"]["item_id"] = "candidate-item-wrong"
        candidate_state_raw = canonical_bytes(candidate_state)
        candidate_host.assets[candidate_state_id] = candidate_state_raw
        candidate_checkpoint["unit_set_hash"] = _sha(candidate_state_raw)
        candidate_checkpoint["checkpoint_hash"] = hash_jcs(
            "checkpoint/v1",
            {key: value for key, value in candidate_checkpoint.items() if key != "checkpoint_hash"},
        )
        candidate_checkpoint_raw = canonical_bytes(candidate_checkpoint)
        candidate_host.assets[candidate_checkpoint_id] = candidate_checkpoint_raw
        candidate_resume["resume_checkpoint_asset_hash"] = _sha(candidate_checkpoint_raw)
        candidate_resume["resume_state_asset_hash"] = _sha(candidate_state_raw)
        with self.assertRaisesRegex(ContractError, "candidate item identity"):
            apply(candidate_resume, candidate_host)


class SnapshotReadBoundaryTests(unittest.TestCase):
    def test_preview_apply_resume_exact_plus_one_unicode_and_counting_host(self) -> None:
        exact_text = "甲乙🙂"
        exact_bytes = len(exact_text.encode("utf-8"))
        exact_preview_host = FrozenRpcHost(exact_text, page_size=3)
        exact_preview_request = _request_for_text(exact_text, max_snapshot_bytes=exact_bytes)
        preview_result = preview(exact_preview_request, exact_preview_host)
        preview_payload = json.loads(exact_preview_host.assets[preview_result["items"][0]["payload_asset_id"]].decode("utf-8"))
        review = build_review_context(preview_payload)

        exact_apply_host = FrozenRpcHost(exact_text, page_size=3)
        exact_apply_request = _request_for_text(
            exact_text,
            capability=CAPABILITY_APPLY,
            review_context=review,
            max_snapshot_bytes=exact_bytes,
        )
        apply(exact_apply_request, exact_apply_host)
        resumed = _resume_request(exact_apply_request, exact_apply_host)
        self.assertEqual(apply(resumed, exact_apply_host)["contract_id"], "candidate-batch/v1")

        over_text = exact_text + "界"
        for capability in (CAPABILITY_PREVIEW, CAPABILITY_APPLY):
            with self.subTest(capability=capability):
                high_host = FrozenRpcHost(over_text, page_size=3)
                high_preview = preview(_request_for_text(over_text, max_snapshot_bytes=exact_bytes + 3), high_host)
                high_payload = json.loads(high_host.assets[high_preview["items"][0]["payload_asset_id"]].decode("utf-8"))
                request = _request_for_text(
                    over_text,
                    capability=capability,
                    review_context=build_review_context(high_payload) if capability == CAPABILITY_APPLY else None,
                    max_snapshot_bytes=exact_bytes,
                )
                counting_host = FrozenRpcHost(over_text, page_size=3)
                with self.assertRaisesRegex(ContractError, "LIMIT_EXCEEDED"):
                    (preview if capability == CAPABILITY_PREVIEW else apply)(request, counting_host)
                reads = [params for method, params in counting_host.calls if method == "host.asset.read/v1"]
                self.assertTrue(reads)
                self.assertLessEqual(max(int(item["offset"]) for item in reads), exact_bytes)
                returned = sum(
                    min(int(item["length"]), counting_host.page_size, len(counting_host.source_asset) - int(item["offset"]))
                    for item in reads
                )
                self.assertEqual(returned, exact_bytes + 1)
                self.assertLess(returned, len(counting_host.source_asset))

        high_resume_host = FrozenRpcHost(over_text, page_size=3)
        high_preview = preview(_request_for_text(over_text, max_snapshot_bytes=exact_bytes + 3), high_resume_host)
        high_payload = json.loads(high_resume_host.assets[high_preview["items"][0]["payload_asset_id"]].decode("utf-8"))
        high_apply_request = _request_for_text(
            over_text,
            capability=CAPABILITY_APPLY,
            review_context=build_review_context(high_payload),
            max_snapshot_bytes=exact_bytes + 3,
        )
        apply(high_apply_request, high_resume_host)
        over_resume = _resume_request(high_apply_request, high_resume_host)
        over_resume["limits"]["max_snapshot_bytes"] = exact_bytes
        reads_before = len([1 for method, _ in high_resume_host.calls if method == "host.asset.read/v1"])
        with self.assertRaisesRegex(ContractError, "LIMIT_EXCEEDED"):
            apply(over_resume, high_resume_host)
        resume_reads = [params for method, params in high_resume_host.calls if method == "host.asset.read/v1"][reads_before:]
        returned = sum(
            min(int(item["length"]), high_resume_host.page_size, len(high_resume_host.source_asset) - int(item["offset"]))
            for item in resume_reads
        )
        self.assertEqual(returned, exact_bytes + 1)
        self.assertLess(returned, len(high_resume_host.source_asset))


if __name__ == "__main__":
    unittest.main()
