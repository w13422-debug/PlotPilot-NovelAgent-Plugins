from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from asset_derivation.contract import (
    DATA_PLUGIN_BY_FORMAT,
    build_data_bundle,
    expected_interpreter_mappings,
    validate_target_payload,
    verify_data_bundle_identity,
    verify_interpreter_mappings,
)
from character_distillation.contract import (
    CharacterContractError,
    EvidenceSpanError,
    apply_conflict_ruling,
    build_evidence_span,
    source_closure,
    validate_archetype,
    validate_source_narrowing,
)


ROOT = Path(__file__).resolve().parents[2]


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _span() -> dict[str, object]:
    return build_evidence_span(
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        node_id="node-1",
        start_codepoint=1,
        end_codepoint=5,
        canonical_text="甲乙丙丁戊己",
        node_range={"start_codepoint": 0, "end_codepoint": 6},
    )


def _target_values() -> dict[str, dict[str, object]]:
    archetype = _json(ROOT / "data" / "character" / "character-archetype" / "v1" / "data" / "archetypes.json")
    world_rules = _json(ROOT / "data" / "world" / "world-rule-template" / "v1" / "data" / "world-rules.json")
    span = _span()
    card = {
        "schema": "character-card/v1",
        "format_id": "character-card/v1",
        "character_id": "char-1",
        "name": "阿宁",
        "identity": "守门人",
        "goals": ["守护城门"],
        "motivations": ["保护家园"],
        "fears": ["失去同伴"],
        "abilities": ["观察"],
        "flaws": ["犹豫"],
        "relationships": [],
        "conflicts": [],
        "arc": {"stage": "setup", "stages": ["setup"], "summary": "尚未改变", "key_events": ["守门"]},
        "speech_habits": ["简短"],
        "behavior_evidence": [span],
        "source_refs": [{"workspace_id": "ws-1", "document_id": "doc-1", "revision_id": "rev-1"}],
        "authority": "candidate_only",
    }
    world_entry = {
        "schema": "world-entry/world",
        "format_id": "world-entry/world",
        "world_id": "world-1",
        "name": "城门",
        "category": "place",
        "description": "城门规则",
        "rules": ["守门"],
        "constraints": ["不得越界"],
        "evidence_spans": [span],
        "source_refs": [{"workspace_id": "ws-1", "document_id": "doc-1", "revision_id": "rev-1"}],
        "authority": "candidate_only",
    }
    plot = {
        "schema": "plot-structure-template/v1",
        "format_id": "plot-structure-template/v1",
        "template_id": "plot-1",
        "version": "1.0.0",
        "scope": "chapter",
        "beats": [{"beat_id": "opening", "ordinal": 1, "label": "开场", "purpose": "建立场景", "required": True, "children": []}],
        "chapter_template": {"opening": "开场", "turning_point": "转折", "climax": "高潮", "ending": "结尾"},
        "metadata": {"description": "一章", "tags": ["test"]},
    }
    return {
        "character-archetype/v1": archetype,
        "world-rule-template/v1": world_rules,
        "plot-structure-template/v1": plot,
        "character-card/v1": card,
        "world-entry/world": world_entry,
        "world-entry/v1": {**world_entry, "schema": "world-entry/v1", "format_id": "world-entry/v1"},
        "world/v1": {**world_entry, "schema": "world/v1", "format_id": "world/v1"},
    }


def test_archetype_is_the_single_closed_data_shape(archetype: dict[str, object]) -> None:
    assert validate_archetype(archetype)["format_id"] == "character-archetype/v1"
    for key in ("dimensions", "atom_kinds", "card_fields", "relation_kinds", "arc_stages"):
        missing = copy.deepcopy(archetype)
        missing.pop(key)
        with pytest.raises(CharacterContractError):
            validate_archetype(missing)
    extra = copy.deepcopy(archetype)
    extra["untrusted"] = True
    with pytest.raises(CharacterContractError):
        validate_archetype(extra)


@pytest.mark.parametrize("format_id", sorted(_target_values()))
def test_every_declared_target_is_loaded_and_semantically_validated(format_id: str) -> None:
    assert validate_target_payload(format_id, _target_values()[format_id])["format_id"] in {
        format_id,
        "world-entry/v1",
        "world-entry/world",
        "world/v1",
    }


@pytest.mark.parametrize("format_id", sorted(_target_values()))
def test_target_junk_missing_extra_foreign_and_semantic_errors_fail_closed(format_id: str) -> None:
    valid = _target_values()[format_id]
    with pytest.raises(Exception):
        validate_target_payload(format_id, {"junk": True})
    missing = copy.deepcopy(valid)
    missing.pop(next(iter(valid)))
    with pytest.raises(Exception):
        validate_target_payload(format_id, missing)
    extra = copy.deepcopy(valid)
    extra["foreign_extra"] = True
    with pytest.raises(Exception):
        validate_target_payload(format_id, extra)
    foreign = copy.deepcopy(valid)
    foreign["format_id"] = "foreign-format/v1"
    with pytest.raises(Exception):
        validate_target_payload(format_id, foreign)
    semantic = copy.deepcopy(valid)
    if format_id == "character-archetype/v1":
        semantic["dimensions"] = [semantic["dimensions"][0], semantic["dimensions"][0]]
    elif format_id == "world-rule-template/v1":
        semantic["rules"] = [semantic["rules"][0], semantic["rules"][0]]
    elif format_id == "plot-structure-template/v1":
        semantic["beats"][0]["ordinal"] = 2
    elif format_id == "character-card/v1":
        semantic["behavior_evidence"][0]["quote_hash"] = "0" * 64
    else:
        semantic["evidence_spans"][0]["quote_hash"] = "0" * 64
    with pytest.raises(Exception):
        validate_target_payload(format_id, semantic)


def test_world_entry_alias_identity_cannot_cross_target_formats() -> None:
    values = _target_values()
    for requested in ("world-entry/v1", "world-entry/world", "world/v1"):
        for foreign in ("world-entry/v1", "world-entry/world", "world/v1"):
            if requested == foreign:
                continue
            payload = copy.deepcopy(values[requested])
            payload["schema"] = foreign
            payload["format_id"] = foreign
            with pytest.raises(Exception):
                validate_target_payload(requested, payload)


def test_evidence_span_recomputes_quote_and_hash_and_rejects_foreign_or_bounds() -> None:
    span = _span()
    assert span["quote"] == "乙丙丁戊"
    assert span["quote_hash"] == hashlib.sha256("乙丙丁戊".encode()).hexdigest()
    from character_distillation.contract import validate_evidence_span, validate_nodes

    nodes = validate_nodes([{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": 6}], 6)
    assert validate_evidence_span(
        span,
        "甲乙丙丁戊己",
        nodes,
        workspace_id="ws-1",
        document_id="doc-1",
        revision_id="rev-1",
        canonical_text_hash=hashlib.sha256("甲乙丙丁戊己".encode()).hexdigest(),
    )
    for mutation in (
        {"document_id": "foreign"},
        {"node_id": "unknown"},
        {"end_codepoint": 99},
        {"quote": "伪造"},
    ):
        bad = copy.deepcopy(span)
        bad.update(mutation)
        with pytest.raises((EvidenceSpanError, CharacterContractError)):
            validate_evidence_span(
                bad,
                "甲乙丙丁戊己",
                nodes,
                workspace_id="ws-1",
                document_id="doc-1",
                revision_id="rev-1",
                canonical_text_hash=hashlib.sha256("甲乙丙丁戊己".encode()).hexdigest(),
            )


def test_conflict_apply_never_narrows_unchanged_value_provenance() -> None:
    conflict = {
        "schema": "character-conflict/v1",
        "conflict_id": "conflict-1",
        "character_id": "char-1",
        "field": "identity",
        "values": [
            {"source_id": "source-a", "value": "同一值", "evidence_spans": [_span()]},
            {"source_id": "source-b", "value": "同一值", "evidence_spans": [_span()]},
        ],
        "authority": "diagnostic_only",
    }
    ruling = {"schema": "character-conflict-ruling/v1", "conflict_id": "conflict-1", "selected_source_ids": ["source-a"], "decision": "select", "rationale": "人工裁决", "actor_id": "user-1", "created_at": "2026-08-30T00:00:00Z"}
    from character_distillation.contract import validate_conflict

    normalized = validate_conflict(conflict, canonical_text="甲乙丙丁戊己", nodes=[{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": 6}], workspace_id="ws-1", document_id="doc-1", revision_id="rev-1", canonical_text_hash=hashlib.sha256("甲乙丙丁戊己".encode()).hexdigest())
    with pytest.raises(CharacterContractError):
        apply_conflict_ruling(normalized, ruling)


def test_synthesize_requires_new_value_or_source_and_source_closure_is_exact() -> None:
    with pytest.raises(CharacterContractError):
        validate_source_narrowing({"value": "x", "source_ids": ["a", "b"]}, {"value": "x", "source_ids": ["a"]})
    with pytest.raises(CharacterContractError):
        validate_source_narrowing({"value": "x", "state": "same", "change": "none", "source_ids": ["a"]}, {"value": "x", "state": "same", "change": "none", "source_ids": ["a"]})
    with pytest.raises(CharacterContractError):
        source_closure([{"atom_id": "atom-1", "source_revision_id": "rev-old"}], accepted_current_ids={"atom-1"}, source_revision_id="rev-new")
    source_closure([{"atom_id": "atom-1", "source_revision_id": "rev-new"}], accepted_current_ids={"atom-1"}, source_revision_id="rev-new")


@pytest.mark.parametrize("format_id", ["character-archetype/v1", "world-rule-template/v1"])
def test_interpreter_mapping_round_trip_is_exact_and_ordered(format_id: str) -> None:
    expected = expected_interpreter_mappings(format_id)
    assert verify_interpreter_mappings(copy.deepcopy(expected), format_id=format_id) == expected
    for mutation in (expected[1:] + expected[:1], expected[:-1], expected + [copy.deepcopy(expected[0])]):
        with pytest.raises(Exception):
            verify_interpreter_mappings(mutation, format_id=format_id)
    foreign = copy.deepcopy(expected)
    foreign[0]["plugin_id"] = "com.foreign.plugin"
    with pytest.raises(Exception):
        verify_interpreter_mappings(foreign, format_id=format_id)


def test_data_bundle_identity_uses_sdk_and_exact_raw_file_set() -> None:
    root = ROOT / "data" / "character" / "character-archetype" / "v1"
    expected = _json(root / "expected.json")
    files = {path: (root / path).read_bytes() for path in expected["package_files"]}
    rows = [{"path": path, "asset_id": f"asset-{index}", "sha256": hashlib.sha256(raw).hexdigest(), "mime": "application/json", "size": len(raw)} for index, (path, raw) in enumerate(files.items())]
    bundle = build_data_bundle(data_plugin_id=expected["plugin_id"], data_release_id=expected["release_id"], package_hash=expected["package_hash"], format_id="character-archetype/v1", root_path="data/archetypes.json", files=rows)
    verify_data_bundle_identity(bundle, version="1.0.0", files_by_path=files)
    with pytest.raises(Exception):
        verify_data_bundle_identity(bundle, version="1.0.0", files_by_path={**files, "extra.json": b"extra"})
    with pytest.raises(Exception):
        build_data_bundle(data_plugin_id="com.foreign.plugin", data_release_id=expected["release_id"], package_hash=expected["package_hash"], format_id="character-archetype/v1", root_path="data/archetypes.json", files=rows)
    assert DATA_PLUGIN_BY_FORMAT["character-archetype/v1"] == expected["plugin_id"]
