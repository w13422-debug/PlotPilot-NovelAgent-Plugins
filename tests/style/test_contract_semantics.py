from __future__ import annotations

import itertools
from copy import deepcopy

import pytest
from style_manufacturing.contract import (
    StyleContractError,
    build_qualification_receipt,
    exact_release_eligible,
    hash_json,
    merge_lexicons,
    style_payload_hash,
    style_release_id,
    validate_lexicon,
    validate_qualification_receipt,
    validate_style_pack,
)


def style_pack(*, version: str = "1.2.3") -> dict:
    value = {
        "schema": "style-pack/v1",
        "style_pack_id": "style-pack:author-a",
        "version": version,
        "style_release_id": "",
        "payload_hash": "",
        "source_cas": {
            "asset_id": "asset:source",
            "sha256": "1" * 64,
            "revision_id": "revision:source-7",
        },
        "target_cas": {
            "workspace_id": "workspace:target",
            "entity_id": "style:author-a",
            "base_revision_id": "revision:target-3",
            "base_content_hash": "2" * 64,
        },
        "manufacture_snapshot": {
            "run_snapshot_hash": "3" * 64,
            "route_id": "route:manufacture",
            "prompt_hash": "4" * 64,
            "output_schema_hash": "5" * 64,
            "chunk_plan_hash": "6" * 64,
            "provider_attempt_ids": ["attempt:manufacture-1", "attempt:manufacture-2"],
            "checkpoint_ids": ["checkpoint:map", "checkpoint:synthesis"],
            "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
        },
        "features": {
            "voice": ["克制", "精确"],
            "rhythm": ["短长交替"],
            "syntax": ["动作前置"],
            "imagery": ["冷色意象"],
            "taboos": ["空泛总结"],
        },
        "lexicon_bindings": [
            {
                "data_plugin_id": "com.example.lexicon-a",
                "data_release_id": "7" * 64,
                "bundle_hash": "8" * 64,
                "order": 1,
            }
        ],
        "constraints": ["保留事实", "禁止模仿署名"],
        "exemplar_hashes": ["9" * 64, "a" * 64],
    }
    value["payload_hash"] = style_payload_hash(value)
    value["style_release_id"] = style_release_id(
        value["style_pack_id"], value["version"], value["payload_hash"]
    )
    return validate_style_pack(value)


def cases(*, passed: bool = True) -> list[dict]:
    return [
        {
            "case_id": f"blind:{index}",
            "writer_output_hash": format(index + 10, "x") * 64,
            "review_output_hash": format(index + 13, "x") * 64,
            "score_0_100": 90 if passed else 20,
            "passed": passed,
        }
        for index in range(3)
    ]


def qualification(pack: dict | None = None, *, passed: bool = True) -> dict:
    value = pack or style_pack()
    return build_qualification_receipt(
        receipt_id="receipt:qualification-1",
        style_pack=value,
        qualification_run_snapshot_hash="b" * 64,
        rubric_hash="c" * 64,
        manufacturing_route_id="route:manufacture",
        writer_route_id="route:writer",
        reviewer_route_id="route:reviewer",
        manufacturing_attempt_ids=["attempt:manufacture-1", "attempt:manufacture-2"],
        writer_attempt_ids=["attempt:writer-1", "attempt:writer-2", "attempt:writer-3"],
        reviewer_attempt_ids=["attempt:reviewer-1", "attempt:reviewer-2", "attempt:reviewer-3"],
        case_results=cases(passed=passed),
        created_at="2026-08-30T00:00:00Z",
    )


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("source CAS", lambda p: p["source_cas"].update(sha256="0" * 64)),
        ("target CAS", lambda p: p["target_cas"].update(base_revision_id="revision:stale")),
        ("route", lambda p: p["manufacture_snapshot"].update(route_id="route:other")),
        ("prompt", lambda p: p["manufacture_snapshot"].update(prompt_hash="0" * 64)),
        ("schema", lambda p: p["manufacture_snapshot"].update(output_schema_hash="0" * 64)),
        ("chunk plan", lambda p: p["manufacture_snapshot"].update(chunk_plan_hash="0" * 64)),
        ("attempt", lambda p: p["manufacture_snapshot"]["provider_attempt_ids"].append("attempt:late")),
        ("checkpoint", lambda p: p["manufacture_snapshot"]["checkpoint_ids"].reverse()),
        ("usage", lambda p: p["manufacture_snapshot"]["usage"].update(total_tokens=999)),
        ("unknown", lambda p: p.update(publication_id="publication:forbidden")),
    ],
)
def test_style_pack_freezes_source_route_prompt_schema_attempts_and_checkpoints(
    label: str, mutate
) -> None:
    value = style_pack()
    mutate(value)
    with pytest.raises(StyleContractError, match="."):
        validate_style_pack(value)


def test_same_path_self_review_and_attempt_overlap_can_never_auto_pass() -> None:
    pack = style_pack()
    for mutation in (
        lambda q: q.update(reviewer_route_id=q["writer_route_id"]),
        lambda q: q.update(reviewer_route_id=q["manufacturing_route_id"]),
        lambda q: q["reviewer_attempt_ids"].append(q["writer_attempt_ids"][0]),
        lambda q: q["writer_attempt_ids"].append(q["manufacturing_attempt_ids"][0]),
    ):
        receipt = qualification(pack)
        mutation(receipt)
        with pytest.raises(StyleContractError, match="path|overlap"):
            validate_qualification_receipt(receipt, pack["style_release_id"])
        assert exact_release_eligible(pack, receipt) is False


def test_only_the_exact_qualified_version_payload_and_release_are_eligible() -> None:
    pack = style_pack(version="1.2.3")
    receipt = qualification(pack)
    assert validate_qualification_receipt(receipt, pack["style_release_id"])["decision"] == "pass"
    assert exact_release_eligible(pack, receipt) is True

    other_version = style_pack(version="1.2.4")
    assert exact_release_eligible(other_version, receipt) is False
    payload_drift = deepcopy(pack)
    payload_drift["constraints"].append("新增约束")
    assert exact_release_eligible(payload_drift, receipt) is False
    assert exact_release_eligible(pack, qualification(pack, passed=False)) is False

    uncovered_attempts = qualification(pack)
    uncovered_attempts["manufacturing_attempt_ids"] = ["attempt:unrelated-manufacture"]
    uncovered_attempts["receipt_hash"] = hash_json(
        "author-style-qualification-receipt/v1",
        {key: value for key, value in uncovered_attempts.items() if key != "receipt_hash"},
    )
    assert exact_release_eligible(pack, uncovered_attempts) is False

    other_manufacturing_route = qualification(pack)
    other_manufacturing_route["manufacturing_route_id"] = "route:unrelated-manufacture"
    other_manufacturing_route["receipt_hash"] = hash_json(
        "author-style-qualification-receipt/v1",
        {key: value for key, value in other_manufacturing_route.items() if key != "receipt_hash"},
    )
    assert exact_release_eligible(pack, other_manufacturing_route) is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(style_release_id="0" * 64),
        lambda r: r.update(style_payload_hash="0" * 64),
        lambda r: r.update(automatic_eligible=False),
        lambda r: r.update(decision="fail"),
        lambda r: r["case_results"].pop(),
        lambda r: r["case_results"][1].update(case_id=r["case_results"][0]["case_id"]),
        lambda r: r["case_results"][0].update(score_0_100=101),
        lambda r: r.update(receipt_hash="0" * 64),
        lambda r: r.update(extra_authority=True),
    ],
)
def test_qualification_receipt_is_complete_closed_and_hash_bound(mutation) -> None:
    pack = style_pack()
    receipt = qualification(pack)
    mutation(receipt)
    with pytest.raises(StyleContractError, match="."):
        validate_qualification_receipt(receipt, pack["style_release_id"])
    assert exact_release_eligible(pack, receipt) is False


def lexicon(lexicon_id: str, plugin_id: str, *, priority: int, preferred: str) -> dict:
    return validate_lexicon(
        {
            "schema": "lexicon/v1",
            "lexicon_id": lexicon_id,
            "version": "1.0.0",
            "normalization": "unicode-nfc-casefold-v1",
            "conflict_policy": "highest-priority-then-plugin-id-byte-order",
            "entries": [
                {
                    "term": "霜刃",
                    "normalized_term": "霜刃",
                    "preferred": preferred,
                    "aliases": ["冰刃"],
                    "forbidden": False,
                    "note": "冲突规则夹具",
                    "priority": priority,
                    "source_plugin_id": plugin_id,
                }
            ],
        }
    )


def test_lexicon_merge_is_permutation_deterministic_and_uses_frozen_conflict_rule() -> None:
    low = lexicon("lexicon:low", "com.example.z", priority=1, preferred="低优先")
    high_z = lexicon("lexicon:high-z", "com.example.z", priority=9, preferred="高优先-Z")
    high_a = lexicon("lexicon:high-a", "com.example.a", priority=9, preferred="高优先-A")
    outputs = [merge_lexicons(order) for order in itertools.permutations([low, high_z, high_a])]
    assert all(value == outputs[0] for value in outputs)
    assert outputs[0]["entries"][0]["preferred"] == "高优先-A"
    assert outputs[0]["entries"][0]["source_plugin_id"] == "com.example.a"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda l: l.update(conflict_policy="last-writer-wins"),
        lambda l: l.update(normalization="platform-default"),
        lambda l: l["entries"][0].update(priority=True),
        lambda l: l["entries"][0].update(database_id="private-authority"),
        lambda l: l["entries"].append(deepcopy(l["entries"][0])),
    ],
)
def test_lexicon_conflict_and_closed_contract_negative_cases_fail_closed(mutation) -> None:
    value = lexicon("lexicon:a", "com.example.a", priority=1, preferred="霜刃")
    mutation(value)
    with pytest.raises(StyleContractError, match="."):
        validate_lexicon(value)
