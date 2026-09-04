from __future__ import annotations

import copy
import hashlib
import json

import pytest

from asset_derivation.contract import validate_target_payload, verify_data_bundle_identity
from asset_derivation.runtime import (
    CAPABILITY_DATA_PACKAGE,
    CAPABILITY_TEMPLATE_DERIVE,
    AssetDerivationPlugin,
)
from character_distillation.contract import (
    build_evidence_span,
    validate_character_atom,
    validate_character_card,
)
from character_distillation.runtime import (
    CAPABILITY_ATOM_EXTRACT,
    CAPABILITY_CARD_GENERATE,
    CAPABILITY_CONFLICT_APPLY,
    CAPABILITY_CONFLICT_REVIEW,
    CharacterDistillationPlugin,
)
from plotpilot_plugin_sdk.verifier import verify_checkpoint, verify_provenance_receipt, verify_result_bundle

from conftest import MemoryHost, asset_request, character_request, package_request, sha256


def _canonical_source(host: MemoryHost, text: str = "阿宁抬头望向城门，随后握紧了手中的信。") -> tuple[str, str]:
    raw = text.encode("utf-8")
    return host.seed_bytes("canonical", raw)


def _atom_asset(host: MemoryHost) -> tuple[str, str]:
    text = "阿宁抬头望向城门，随后握紧了手中的信。"
    text_hash = sha256(text.encode("utf-8"))
    span = build_evidence_span(
        workspace_id="ws-character",
        document_id="doc-character",
        revision_id="rev-character",
        node_id="node-1",
        start_codepoint=0,
        end_codepoint=len(text),
        canonical_text=text,
        node_range={"start_codepoint": 0, "end_codepoint": len(text)},
    )
    from character_distillation.runtime import PACKAGE_HASH, RELEASE_ID

    payload = {
        "schema": "character-atom/v1",
        "atom_id": "atom-input",
        "character_id": "character-ning",
        "dimension": "identity",
        "kind": "identity",
        "observation": "阿宁守在城门前",
        "interpretation": "角色承担守门职责",
        "confidence": "confirmed",
        "source_revision_id": "rev-character",
        "evidence_spans": [span],
        "provenance": {
            "plugin_id": "com.plotpilot.novelagent.character-distillation",
            "capability_id": CAPABILITY_ATOM_EXTRACT,
            "package_hash": PACKAGE_HASH,
            "release_id": RELEASE_ID,
            "run_snapshot_hash": "1" * 64,
            "model_receipt_id": None,
        },
        "authority": "candidate_only",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return host.seed_bytes("atom", raw)


def _conflict_assets(host: MemoryHost) -> tuple[str, str, str, str]:
    text = "阿宁抬头望向城门，随后握紧了手中的信。"
    span = build_evidence_span(
        workspace_id="ws-character",
        document_id="doc-character",
        revision_id="rev-character",
        node_id="node-1",
        start_codepoint=0,
        end_codepoint=len(text),
        canonical_text=text,
        node_range={"start_codepoint": 0, "end_codepoint": len(text)},
    )
    conflict = {
        "schema": "character-conflict/v1",
        "conflict_id": "conflict-1",
        "character_id": "character-ning",
        "field": "identity",
        "values": [
            {"source_id": "source-a", "value": "守门人", "evidence_spans": [span]},
            {"source_id": "source-b", "value": "信使", "evidence_spans": [span]},
        ],
        "authority": "diagnostic_only",
    }
    ruling = {
        "schema": "character-conflict-ruling/v1",
        "conflict_id": "conflict-1",
        "selected_source_ids": ["source-a"],
        "decision": "select",
        "rationale": "用户选择有直接证据的身份",
        "actor_id": "user-1",
        "created_at": "2026-08-30T00:00:00Z",
    }
    conflict_id, conflict_hash = host.seed_json("conflict", conflict)
    ruling_id, ruling_hash = host.seed_json("ruling", ruling)
    return conflict_id, conflict_hash, ruling_id, ruling_hash


def _assert_failed(
    result: dict[str, object] | None,
    host: MemoryHost,
    *,
    stage_count: int = 0,
    completion_count: int | None = None,
) -> None:
    assert result is not None
    assert result["contract_id"] == "diagnostic-bundle/v1"
    assert result["bundle_type"] == "diagnostic"
    assert len(host.stage_calls) == stage_count
    if completion_count is None:
        assert host.completion_calls and host.completion_calls[-1]["outcome"] == "failed"
    else:
        assert len(host.completion_calls) == completion_count + 1
        assert host.completion_calls[-1]["outcome"] == "failed"


def test_character_atom_run_consumes_closed_archetype_and_proves_runtime_participation(host: MemoryHost) -> None:
    request = character_request(host, CAPABILITY_ATOM_EXTRACT)
    plugin = CharacterDistillationPlugin()
    result = plugin.run(request, host)
    assert result is not None and result["contract_id"] == "candidate-batch/v1"
    verify_result_bundle(result, snapshot_workspace_id="ws-character", snapshot_hash_value=request["run_snapshot_hash"])
    assert len(host.stage_calls) == 1
    staged_bundle = json.loads(host.assets[str(host.stage_calls[0]["result_bundle_asset_id"])].decode("utf-8"))
    assert staged_bundle == result
    item = result["items"][0]
    payload = json.loads(host.assets[str(item["payload_asset_id"])].decode("utf-8"))
    archetype = json.loads((__import__("pathlib").Path("data/character/character-archetype/v1/data/archetypes.json")).read_text(encoding="utf-8"))
    validate_character_atom(payload, archetype=archetype, canonical_text="阿宁抬头望向城门，随后握紧了手中的信。", nodes=request["nodes"], workspace_id=request["workspace_id"], document_id=request["document_id"], revision_id=request["source_revision_id"], canonical_text_hash=request["canonical_text_hash"])
    assert plugin.last_stage_response["staged_items"][0]["candidate_id"].startswith("candidate-")
    verify_checkpoint(plugin.last_checkpoint, expected_snapshot_hash=request["run_snapshot_hash"])
    verify_provenance_receipt(plugin.last_receipt)


def test_character_card_requires_and_consumes_a_closed_atom(host: MemoryHost) -> None:
    _atom_asset(host)
    request = character_request(host, CAPABILITY_CARD_GENERATE, atom_asset_id="atom")
    request["atom_asset_hash"] = sha256(host.assets["atom"])
    plugin = CharacterDistillationPlugin()
    result = plugin.run(request, host)
    assert result is not None and result["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[str(result["items"][0]["payload_asset_id"])].decode("utf-8"))
    archetype = json.loads((__import__("pathlib").Path("data/character/character-archetype/v1/data/archetypes.json")).read_text(encoding="utf-8"))
    validate_character_card(payload, archetype=archetype, canonical_text="阿宁抬头望向城门，随后握紧了手中的信。", nodes=request["nodes"], workspace_id=request["workspace_id"], document_id=request["document_id"], revision_id=request["source_revision_id"], canonical_text_hash=request["canonical_text_hash"])


def test_conflict_review_is_diagnostic_and_apply_requires_explicit_ruling(host: MemoryHost) -> None:
    conflict_id, conflict_hash, ruling_id, ruling_hash = _conflict_assets(host)
    review_request = character_request(host, CAPABILITY_CONFLICT_REVIEW, conflict_asset_id=conflict_id, conflict_asset_hash=conflict_hash)
    review = CharacterDistillationPlugin().run(review_request, host)
    assert review is not None and review["contract_id"] == "diagnostic-bundle/v1"
    assert host.stage_calls == []
    apply_request = character_request(host, CAPABILITY_CONFLICT_APPLY, conflict_asset_id=conflict_id, conflict_asset_hash=conflict_hash, ruling_asset_id=ruling_id, ruling_asset_hash=ruling_hash)
    applied = CharacterDistillationPlugin().run(apply_request, host)
    assert applied is not None and applied["contract_id"] == "candidate-batch/v1"
    assert len(host.stage_calls) == 1
    payload = json.loads(host.assets[str(applied["items"][0]["payload_asset_id"])].decode("utf-8"))
    assert payload["authority"] == "candidate_only" and payload["ruling"]["decision"] == "select"


@pytest.mark.parametrize("mutation", ["dimensions", "evidence", "foreign_node", "quote"])
def test_character_closed_source_negative_cases_fail_before_stage(host: MemoryHost, mutation: str) -> None:
    request = character_request(host, CAPABILITY_ATOM_EXTRACT)
    if mutation == "dimensions":
        raw = json.loads(host.assets["archetype"].decode("utf-8"))
        raw.pop("dimensions")
        _id, digest = host.seed_json("archetype-bad", raw)
        request.update(archetype_asset_id="archetype-bad", archetype_asset_hash=digest)
    elif mutation == "evidence":
        # A node range that exceeds the canonical Revision must be rejected
        # before an EvidenceSpan/Candidate can be produced.
        request["nodes"] = [{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": 10_000}]
    elif mutation == "foreign_node":
        # Duplicate/foreign node identities are not a valid closed Node set;
        # this exercises the unknown-node side of the EvidenceSpan gate.
        request["nodes"] = [
            {"node_id": "node-1", "start_codepoint": 0, "end_codepoint": len("阿宁抬头望向城门，随后握紧了手中的信。")},
            {"node_id": "node-1", "start_codepoint": 0, "end_codepoint": 1},
        ]
    else:
        # The generated Atom always quotes the exact source, so a malformed
        # source hash is the fail-closed equivalent of a forged quote.
        request["canonical_text_hash"] = "0" * 64
    _assert_failed(CharacterDistillationPlugin().run(request, host), host)


def test_character_cancel_and_validate_do_not_stage(host: MemoryHost) -> None:
    cancel = character_request(host, CAPABILITY_ATOM_EXTRACT, operation="cancel")
    assert CharacterDistillationPlugin().run(cancel, host) is None
    assert host.completion_calls[-1]["outcome"] == "cancelled"
    validate = character_request(host, CAPABILITY_ATOM_EXTRACT, operation="run")
    validate["operation"] = "validate"
    result = CharacterDistillationPlugin().run(validate, host)
    assert result is not None and host.stage_calls == []


def test_character_resume_exact_checkpoint_state_bundle_binding_and_tamper_rejection(host: MemoryHost) -> None:
    request = character_request(host, CAPABILITY_ATOM_EXTRACT)
    plugin = CharacterDistillationPlugin()
    original = plugin.run(request, host)
    assert original is not None
    resume = copy.deepcopy(request)
    resume["operation"] = "resume"
    resume["resume_checkpoint_asset_id"] = "asset-" + sha256(json.dumps(plugin.last_checkpoint, sort_keys=True, separators=(",", ":")).encode())[:48]
    # The Host stores the exact canonical upload bytes; locate the checkpoint
    # by its identity rather than relying on the upload naming implementation.
    resume["resume_checkpoint_asset_id"] = next(a for a, raw in host.assets.items() if raw.startswith(b'{"checkpoint_hash"') or b'"schema":"checkpoint/v1"' in raw)
    resume["resume_checkpoint_asset_hash"] = sha256(host.assets[resume["resume_checkpoint_asset_id"]])
    state_id = plugin.last_checkpoint["state_asset_id"]
    resume["resume_state_asset_id"] = state_id
    resume["resume_state_asset_hash"] = sha256(host.assets[state_id])
    resumed = CharacterDistillationPlugin().run(resume, host)
    assert resumed == original
    # Replace the state with an externally labelled state.  The checkpoint
    # still points to the original state, so no stage is reachable.
    bad_state = json.loads(host.assets[state_id].decode("utf-8"))
    bad_state["capability_id"] = "foreign.capability/v1"
    bad_raw = json.dumps(bad_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    bad_id, bad_hash = host.seed_bytes("bad-state", bad_raw)
    tampered = copy.deepcopy(resume)
    tampered["resume_state_asset_id"] = bad_id
    tampered["resume_state_asset_hash"] = bad_hash
    failed = CharacterDistillationPlugin().run(tampered, host)
    _assert_failed(failed, host, stage_count=2, completion_count=2)


@pytest.mark.parametrize("target_format", ["character-archetype/v1", "character-card/v1", "world-rule-template/v1", "world-entry/v1", "world-entry/world", "world/v1", "plot-structure-template/v1"])
def test_asset_template_derive_generates_and_validates_each_frozen_target(host: MemoryHost, target_format: str) -> None:
    request = asset_request(host, target_format_id=target_format)
    result = AssetDerivationPlugin().run(request, host)
    assert result is not None and result["contract_id"] == "candidate-batch/v1"
    payload = json.loads(host.assets[str(result["items"][0]["payload_asset_id"])].decode("utf-8"))
    validate_target_payload(target_format, payload)
    assert len(host.stage_calls) == 1


def test_asset_template_junk_projection_or_foreign_target_fails_before_candidate(host: MemoryHost) -> None:
    request = asset_request(host)
    raw = b'{"junk":true}'
    host.seed_bytes("junk", raw)
    request.update(source_asset_id="junk", source_asset_hash=sha256(raw), source_projection={"source_format_id": "character-archetype/v1", "payload": {"junk": True}})
    failed = AssetDerivationPlugin().run(request, host)
    _assert_failed(failed, host)
    host = MemoryHost()
    request = asset_request(host, target_format_id="foreign-format/v1")
    _assert_failed(AssetDerivationPlugin().run(request, host), host)


def test_data_package_runtime_uses_public_bundle_verifier_and_sdk_identity(host: MemoryHost) -> None:
    request = package_request(host)
    plugin = AssetDerivationPlugin()
    result = plugin.run(request, host)
    assert result is not None and result["contract_id"] == "artifact-bundle/v1"
    assert host.stage_calls == []
    artifact = result["items"][0]
    bundle = json.loads(host.assets[str(artifact["payload_asset_id"])].decode("utf-8"))
    assert bundle["schema"] == "plugin-data-bundle/v1"
    files = {row["path"]: host.assets[row["asset_id"]] for row in bundle["files"]}
    verify_data_bundle_identity(bundle, version="1.0.0", files_by_path=files)
    assert plugin.last_receipt["staged_items"] == []


@pytest.mark.parametrize("mutation", ["package_hash", "mapping_order", "duplicate_path", "foreign_plugin"])
def test_data_package_identity_mapping_and_foreign_inputs_fail_closed(host: MemoryHost, mutation: str) -> None:
    request = package_request(host)
    if mutation == "package_hash":
        request["package_hash"] = "0" * 64
    elif mutation == "mapping_order":
        request["interpreter_mappings"] = list(reversed(request["interpreter_mappings"]))
    elif mutation == "duplicate_path":
        request["files"] = request["files"] + [copy.deepcopy(request["files"][0])]
    else:
        request["data_plugin_id"] = "com.foreign.data"
    _assert_failed(AssetDerivationPlugin().run(request, host), host)


def test_asset_cancel_and_resume_state_binding(host: MemoryHost) -> None:
    cancel = asset_request(host, operation="run")
    cancel["operation"] = "cancel"
    assert AssetDerivationPlugin().run(cancel, host) is None
    # Template derive is the Asset capability that advertises resume.  A
    # normal run persists a checkpoint and exact state/bundle bindings.
    host = MemoryHost()
    plugin = AssetDerivationPlugin()
    request = asset_request(host)
    original = plugin.run(request, host)
    assert original is not None and plugin.last_checkpoint is not None
    resume = copy.deepcopy(request)
    resume["operation"] = "resume"
    checkpoint_id = plugin.last_checkpoint["state_asset_id"]
    # Locate the uploaded checkpoint by its decoded schema.
    checkpoint_asset_id = next(a for a, raw in host.assets.items() if b'"checkpoint/v1"' in raw)
    resume.update({"resume_checkpoint_asset_id": checkpoint_asset_id, "resume_checkpoint_asset_hash": sha256(host.assets[checkpoint_asset_id]), "resume_state_asset_id": checkpoint_id, "resume_state_asset_hash": sha256(host.assets[checkpoint_id])})
    resumed = AssetDerivationPlugin().run(resume, host)
    assert resumed == original
    bad = copy.deepcopy(resume)
    bad["resume_state_asset_hash"] = "0" * 64
    _assert_failed(AssetDerivationPlugin().run(bad, host), host, stage_count=2, completion_count=2)
