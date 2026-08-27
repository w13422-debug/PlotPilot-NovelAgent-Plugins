from __future__ import annotations

import hashlib
import json
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode  # noqa: E402
from plotpilot_plugin_sdk.fake_provider import FakeProvider  # noqa: E402
from plotpilot_plugin_sdk.framing import FrameDecoder, decode_frame, encode_frame  # noqa: E402
from plotpilot_plugin_sdk.package import (  # noqa: E402
    build_files_sha256,
    digest_package,
    package_hash,
    skill_package_hash,
    skill_release_id,
)
from plotpilot_plugin_sdk.rpc import ChunkUploadLedger, OperationLedger  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    build_claim_input_asset,
    request_key,
    snapshot_hash,
    verify_catalog,
    verify_claim_input,
    verify_claim_input_asset,
    verify_data_bundle,
    verify_data_interpreter_binding,
    verify_evidence_span,
    verify_generation,
    verify_generation_unchanged,
    verify_lifecycle_transition,
    verify_release_pin,
    verify_release_retirement,
    verify_plan,
    verify_result_bundle,
    verify_skill_chain,
    verify_skill_receipt,
    verify_snapshot,
)


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_package_skill_and_data_hash_receipts_match_goldens() -> None:
    package_dir = ROOT / "contracts" / "golden" / "package"
    package_files = {
        "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
        "plugin.json": (package_dir / "plugin.json").read_bytes(),
    }
    package_expected = _read_json(package_dir / "expected.json")
    package_digest = digest_package(package_files, "com.plotpilot.golden.echo", "1.0.0")
    assert package_digest.files_sha256.decode("utf-8") == package_expected["files_sha256"]
    assert package_digest.package_hash == package_expected["package_hash"]
    assert package_digest.release_id == package_expected["release_id"]
    assert package_hash(package_files) == "987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068"

    skill_dir = ROOT / "contracts" / "golden" / "skill"
    skill_files = {
        "prompt.txt": (skill_dir / "prompt.txt").read_bytes(),
        "skill.json": (skill_dir / "skill.json").read_bytes(),
    }
    skill_expected = _read_json(skill_dir / "expected.json")
    assert build_files_sha256(skill_files).decode("utf-8") == skill_expected["files_sha256"]
    assert skill_package_hash(skill_files) == skill_expected["skill_package_hash"]
    assert skill_release_id("com.plotpilot.skill.golden", "1.0.0", skill_package_hash(skill_files)) == skill_expected["skill_release_id"]


def test_run_snapshot_result_data_and_skill_goldens_verify() -> None:
    snapshot = _read_json(ROOT / "contracts" / "golden" / "run-snapshot" / "snapshot.json")
    expected = _read_json(ROOT / "contracts" / "golden" / "run-snapshot" / "expected.json")
    verify_snapshot(snapshot)
    assert request_key(snapshot) == expected["request_key"]
    assert snapshot_hash(snapshot) == expected["snapshot_hash"]

    bundle = _read_json(ROOT / "contracts" / "examples" / "result-bundle.json")
    verify_result_bundle(bundle, snapshot_workspace_id=snapshot["workspace_id"], snapshot_hash_value=snapshot["snapshot_hash"])
    verify_data_bundle(_read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-data-bundle.json"))

    receipt = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "skill-run-receipt.json")
    chain = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "skill-chain-result.json")
    verify_skill_receipt(receipt)
    verify_skill_chain(chain, [receipt])


def test_rpc_framing_operation_idempotency_and_upload_recovery() -> None:
    frame = encode_frame({"jsonrpc": "2.0", "method": "runtime.heartbeat"})
    decoder = FrameDecoder()
    assert decoder.feed(frame[:10]) == []
    assert decoder.feed(frame[10:]) == [{"jsonrpc": "2.0", "method": "runtime.heartbeat"}]
    with pytest.raises(ContractError):
        decode_frame(frame + b"x")

    ledger = OperationLedger()
    calls = 0

    def action() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"accepted": True, "value": "stable"}

    first = ledger.apply_frame("attempt-1", "host.job.event/v1", "op-1", {"x": 1}, action, response_id="123e4567-e89b-12d3-a456-426614174000")
    assert ledger.apply_frame("attempt-1", "host.job.event/v1", "op-1", {"x": 1}, action, response_id="different") == first
    assert calls == 1
    with pytest.raises(ContractError) as caught:
        ledger.apply("attempt-1", "host.job.event/v1", "op-1", {"x": 2}, action)
    assert caught.value.code == ErrorCode.DUPLICATE_REQUEST

    content = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    upload = ChunkUploadLedger()
    result = upload.create(operation_key="op-1", upload_id="upload-1", offset=0, total_size=5, expected_hash=digest, chunk_hash=digest, base64_chunk="aGVsbG8=", final=True)
    assert result["completed"] is True
    assert upload.status("upload-1", digest)["asset_id"] == "asset-upload-upload-1"


def test_fake_provider_is_deterministic_and_supports_stream_lifecycle() -> None:
    provider = FakeProvider()
    run = provider.start("invocation-1", "request", chunks=("a", "b"))
    assert [chunk.text for chunk in provider.stream(run.run_id)] == ["a", "ab"]
    receipt = provider.receipt(run.run_id)
    assert receipt.state == "succeeded"
    assert receipt.response_hash == hashlib.sha256(b"ab").hexdigest()


def test_catalog_data_interpreter_and_ui_bindings_are_bidirectional() -> None:
    catalog = _read_json(ROOT / "catalog" / "plugin-catalog-v1.json")
    verify_catalog(catalog)
    verify_data_interpreter_binding(
        "style-pack/v1",
        "com.plotpilot.novelagent.chapter-workflow",
        "writing.chapter.draft/v1",
        catalog=catalog,
    )
    broken = copy.deepcopy(catalog)
    broken["code_plugins"][0]["capabilities"][0]["ui_contributions"][0]["capability_id"] = "wrong/v1"
    with pytest.raises(ContractError):
        verify_catalog(broken)
    plan = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json")
    plan["data_bindings"] = [{
        "data_binding_id": "data-binding-1",
        "data_plugin_id": "com.plotpilot.data.style",
        "release_requirement": "1.0.0",
        "format_id": "style-pack/v1",
        "interpreter_binding_id": "interpreter-1",
        "order": 10,
        "enabled": True,
        "parameters_asset_id": None,
    }]
    verify_plan(plan, catalog=catalog, interpreter_bindings={"interpreter-1": {"plugin_id": "com.plotpilot.novelagent.chapter-workflow", "capability_id": "writing.chapter.draft/v1"}})
    with pytest.raises(ContractError):
        verify_plan(plan, catalog=catalog, interpreter_bindings={"interpreter-1": {"plugin_id": "com.plotpilot.novelagent.chapter-workflow", "capability_id": "writing.chapter.outline/v1"}})


def test_claim_input_and_evidence_span_bind_ordered_atoms_to_run_snapshot() -> None:
    canonical_text = "A😀BC"
    span = {
        "schema": "evidence-span/v1",
        "workspace_id": "ws-1",
        "document_id": "doc-a",
        "revision_id": "rev-a",
        "node_id": "node-a",
        "start_codepoint": 1,
        "end_codepoint": 2,
        "quote": "😀",
        "quote_hash": hashlib.sha256("😀".encode("utf-8")).hexdigest(),
        "canonical_text_hash": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
    }
    verify_evidence_span(span, canonical_text, {"start_codepoint": 0, "end_codepoint": 4})
    atom = {
        "ordinal": 0,
        "atom_id": "atom-1",
        "payload_hash": "1" * 64,
        "acceptance_ordinal": 1,
        "evidence_spans": [span],
    }
    raw, asset_hash = build_claim_input_asset("rev-a", [atom])
    accepted = {"atom-1": {"current": True, "accepted": True, "revision_id": "rev-a", "payload_hash": "1" * 64, "acceptance_ordinal": 1}}
    verify_claim_input(json.loads(raw), accepted_atoms=accepted, canonical_text=canonical_text)
    snapshot = _read_json(ROOT / "contracts" / "golden" / "run-snapshot" / "snapshot.json")
    snapshot["asset_hashes"] = [{**item, "sha256": asset_hash if item["asset_id"] == "asset-params" else item["sha256"]} for item in snapshot["asset_hashes"]]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    parsed = verify_claim_input_asset(raw, snapshot, accepted_atoms=accepted, canonical_text=canonical_text)
    assert parsed["ordered_atoms"][0]["atom_id"] == "atom-1"
    bad = copy.deepcopy(span)
    bad["quote_hash"] = "0" * 64
    with pytest.raises(ContractValidationError):
        verify_evidence_span(bad, canonical_text)


def test_generation_lifecycle_retirement_pin_and_rollback_semantics() -> None:
    generation = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-generation.json")
    verify_generation(generation)
    verify_generation_unchanged(generation, copy.deepcopy(generation))
    selected = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-lifecycle-transition.json")
    verify_lifecycle_transition(selected)
    retiring = {**selected, "state": "failed", "failure_code": "fixture-failure"}
    verify_lifecycle_transition(retiring, previous=selected)
    retirement = _read_json(ROOT / "contracts" / "examples" / "release-retirement.json") if (ROOT / "contracts" / "examples" / "release-retirement.json").exists() else _read_json(ROOT / "contracts" / "examples" / "fixtures" / "release-retirement.json")
    pin = _read_json(ROOT / "contracts" / "examples" / "fixtures" / "release-pin.json")
    verify_release_retirement(retirement)
    verify_release_pin(pin, retirement=retirement)
