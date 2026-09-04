from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
for path in (
    ROOT / "sdk",
    ROOT / "plugins" / "narrative-analysis",
    ROOT / "plugins" / "outline-projection",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from narrative_analysis.contract import (  # noqa: E402
    EvidenceSpanError,
    NarrativeContractError,
    build_evidence_span,
    canonical_json_bytes,
    compile_narrative_plan,
    sha256_text,
    validate_narrative_plan,
    validate_narrative_synthesis,
    validate_narrative_unit,
    validate_nodes,
)
from outline_projection.runtime import ProjectionError, render_projection  # noqa: E402


H = "a" * 64
DATA_IDENTITY = json.loads(
    (ROOT / "data" / "plot-structure" / "v1" / "identity.json").read_text(encoding="utf-8")
)


def narrative_values() -> tuple[dict, dict, dict]:
    text = "甲🙂乙转折丙"
    nodes = validate_nodes(
        [{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": len(text)}],
        len(text),
    )
    span = {
        "evidence_span_id": "span-1",
        **build_evidence_span(
            workspace_id="workspace-1",
            document_id="document-1",
            revision_id="revision-1",
            node_id="node-1",
            start_codepoint=1,
            end_codepoint=5,
            canonical_text=text,
        ),
    }
    attribution = {
        "attribution_id": "attribution-1",
        "source_type": "canonical_revision",
        "workspace_id": "workspace-1",
        "source_id": "document-1",
        "revision_or_hash": "revision-1",
        "evidence_span_ids": ["span-1"],
        "child_receipt_id": "child-receipt-1",
    }
    unit = {
        "schema": "narrative-unit/v1",
        "unit_id": "unit-1",
        "unit_kind": "turn",
        "title": "转折",
        "summary": "证据绑定剧情转折",
        "order": 0,
        "evidence_spans": [span],
        "source_attributions": [attribution],
        "relations": [],
        "provenance": {"source_mode": "broker", "model_receipt_id": None},
    }
    plan = compile_narrative_plan(
        plan_id="plan-1",
        version=1,
        template_binding={
            "data_plugin_id": "com.plotpilot.novelagent.plot-structure-template",
            "data_release_id": DATA_IDENTITY["release_id"],
            "package_hash": DATA_IDENTITY["package_hash"],
            "format_id": "plot-structure-template/v1",
            "template_id": "three-act",
        },
        unit_refs=[{
            "unit_id": "unit-1",
            "payload_asset_id": "asset-unit-1",
            "payload_hash": hashlib.sha256(canonical_json_bytes(unit)).hexdigest(),
            "order": 0,
            "evidence_span_ids": ["span-1"],
            "source_attribution_ids": ["attribution-1"],
        }],
        hierarchy=[{
            "node_id": "book-1",
            "parent_node_id": None,
            "level": "book",
            "title": "全书",
            "unit_ids": ["unit-1"],
            "order": 0,
        }],
        run_snapshot_hash="e" * 64,
    )
    synthesis = {
        "schema": "narrative-synthesis/v1",
        "synthesis_id": "synthesis-1",
        "title": "综合",
        "summary": "可追溯综合",
        "plan_ref": {
            "plan_id": "plan-1",
            "payload_asset_id": "asset-plan-1",
            "payload_hash": "f" * 64,
        },
        "sections": [{
            "section_id": "section-1",
            "title": "第一节",
            "summary": "转折",
            "unit_ids": ["unit-1"],
            "evidence_span_ids": ["span-1"],
            "source_attribution_ids": ["attribution-1"],
        }],
        "evidence_spans": [span],
        "source_attributions": [attribution],
        "broker_children": [{
            "binding_id": "binding-1",
            "child_job_id": "child-job-1",
            "child_run_snapshot_hash": "1" * 64,
            "result_contract": "candidate-batch/v1",
            "result_bundle_asset_id": "asset-child-bundle-1",
            "result_bundle_hash": "2" * 64,
            "provenance_receipt_id": "child-receipt-1",
            "broker_invocation_asset_id": "asset-broker-invocation-1",
            "broker_invocation_hash": "3" * 64,
        }],
    }
    return unit, plan, synthesis


def test_evidence_span_uses_unicode_codepoints_and_exact_revision_binding() -> None:
    unit, _, _ = narrative_values()
    span = unit["evidence_spans"][0]
    assert span["quote"] == "🙂乙转折"
    assert span["quote_hash"] == sha256_text(span["quote"])
    assert span["canonical_text_hash"] == sha256_text("甲🙂乙转折丙")
    broken = deepcopy(unit)
    broken["evidence_spans"][0]["quote"] = "乙转折"
    with pytest.raises(EvidenceSpanError):
        validate_narrative_unit(
            broken,
            canonical_text="甲🙂乙转折丙",
            nodes=[{"node_id": "node-1", "start_codepoint": 0, "end_codepoint": 7}],
            workspace_id="workspace-1",
            document_id="document-1",
            revision_id="revision-1",
            canonical_text_hash=sha256_text("甲🙂乙转折丙"),
        )


def test_narrative_unit_requires_closed_evidence_and_per_source_attribution() -> None:
    unit, _, _ = narrative_values()
    assert validate_narrative_unit(unit) == unit
    for mutate in (
        lambda value: value.update(unknown_authority=True),
        lambda value: value.update(evidence_spans=[]),
        lambda value: value["source_attributions"][0].update(evidence_span_ids=["missing"]),
    ):
        broken = deepcopy(unit)
        mutate(broken)
        with pytest.raises(NarrativeContractError):
            validate_narrative_unit(broken)


def test_business_plan_is_immutable_deterministic_and_not_plugin_plan() -> None:
    _, plan, _ = narrative_values()
    assert validate_narrative_plan(plan) == plan
    assert canonical_json_bytes(plan) == canonical_json_bytes(deepcopy(plan))
    assert hashlib.sha256(canonical_json_bytes(plan)).hexdigest() == hashlib.sha256(
        canonical_json_bytes(validate_narrative_plan(plan))
    ).hexdigest()
    for schema, immutable in (("plugin-plan/v1", True), ("narrative-plan/v1", False)):
        broken = deepcopy(plan)
        broken["schema"] = schema
        broken["immutable"] = immutable
        with pytest.raises(NarrativeContractError):
            validate_narrative_plan(broken)


def test_synthesis_closes_evidence_attribution_and_broker_receipts() -> None:
    _, _, synthesis = narrative_values()
    assert validate_narrative_synthesis(synthesis) == synthesis
    broken = deepcopy(synthesis)
    broken["broker_children"] = []
    with pytest.raises(NarrativeContractError):
        validate_narrative_synthesis(broken)
    broken = deepcopy(synthesis)
    broken["source_attributions"][0]["child_receipt_id"] = "unbound-receipt"
    with pytest.raises(NarrativeContractError):
        validate_narrative_synthesis(broken)


def test_projection_is_byte_deterministic_read_only_and_preserves_source_closure() -> None:
    unit, plan, synthesis = narrative_values()
    before = deepcopy((unit, plan, synthesis))
    first = render_projection(
        synthesis,
        plan,
        [unit],
        source_asset_id="asset-synthesis-1",
        source_asset_hash=H,
    )
    second = render_projection(
        synthesis,
        plan,
        [unit],
        source_asset_id="asset-synthesis-1",
        source_asset_hash=H,
        views=["relation", "timeline", "tree", "card"],
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert (unit, plan, synthesis) == before
    assert [item["mode"] for item in first["views"]] == ["tree", "card", "timeline", "relation"]
    assert first["evidence_spans"] == synthesis["evidence_spans"]
    assert first["source_attributions"] == synthesis["source_attributions"]
    assert first["broker_children"] == synthesis["broker_children"]
    assert first["authority"] == {
        "mode": "read_only_projection",
        "authoritative_source": "narrative-synthesis/v1",
        "creates_second_authority": False,
        "free_form_canvas": False,
    }


@pytest.mark.parametrize("views", [[], ["canvas"], ["tree", "tree"]])
def test_projection_rejects_free_form_or_ambiguous_views(views: list[str]) -> None:
    unit, plan, synthesis = narrative_values()
    with pytest.raises(ProjectionError):
        render_projection(
            synthesis,
            plan,
            [unit],
            source_asset_id="asset-synthesis-1",
            source_asset_hash=H,
            views=views,
        )


def test_projection_fails_closed_when_plan_unit_closure_is_missing() -> None:
    unit, plan, synthesis = narrative_values()
    plan["hierarchy"][0]["unit_ids"] = ["unit-missing"]
    with pytest.raises(ProjectionError, match="missing"):
        render_projection(
            synthesis,
            plan,
            [unit],
            source_asset_id="asset-synthesis-1",
            source_asset_hash=H,
        )
