from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk import (  # noqa: E402
    CandidateStagingStore,
    verify_result_bundle,
)
from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )


def _candidate_cases() -> tuple[list[dict[str, Any]], list[bool]]:
    base = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    cases: list[dict[str, Any]] = []
    expected: list[bool] = []

    def add(label: str, value: dict[str, Any], workspace: str | None, known: list[str], accepts: bool) -> None:
        cases.append({"label": label, "value": value, "workspace": workspace, "known_parent_ids": known})
        expected.append(accepts)

    add("valid-complete", copy.deepcopy(base), "ws-1", [], True)

    add("missing-workspace", copy.deepcopy(base), None, [], False)

    unknown_parent = copy.deepcopy(base)
    unknown_parent["items"][0]["parent_candidate_ids"] = ["candidate-not-visible"]
    add("unknown-parent", unknown_parent, "ws-1", [], False)

    known_parent = copy.deepcopy(unknown_parent)
    add("known-parent-in-workspace", known_parent, "ws-1", ["candidate-not-visible"], True)

    for status in ("failed", "skipped"):
        rejected = copy.deepcopy(base)
        rejected["items"][0]["status"] = status
        rejected["partial"] = True
        add(f"{status}-candidate", rejected, "ws-1", [], False)

    partial_item = copy.deepcopy(base)
    partial_item["items"][0]["status"] = "partial"
    partial_item["partial"] = True
    add("partial-item-with-partial-bundle", partial_item, "ws-1", [], True)

    partial_flag_without_item = copy.deepcopy(base)
    partial_flag_without_item["partial"] = True
    add("partial-bundle-with-complete-item", partial_flag_without_item, "ws-1", [], False)

    complete_flag_with_partial = copy.deepcopy(partial_item)
    complete_flag_with_partial["partial"] = False
    add("complete-bundle-with-partial-item", complete_flag_with_partial, "ws-1", [], False)

    bad_incomplete_schema = copy.deepcopy(partial_item)
    bad_incomplete_schema["items"][0]["item_kind"] = "incomplete_stream"
    bad_incomplete_schema["items"][0]["mutation"]["payload_schema"] = "plugin/unknown-payload/v1"
    add("incomplete-stream-wrong-payload-schema", bad_incomplete_schema, "ws-1", [], False)

    cross_workspace_write = copy.deepcopy(base)
    cross_workspace_write["items"][0]["write_set"][0]["workspace_id"] = "ws-other"
    add("cross-workspace-write-set", cross_workspace_write, "ws-1", [], False)
    return cases, expected


def _python_accepts(case: dict[str, Any]) -> bool:
    try:
        verify_result_bundle(
            case["value"],
            snapshot_workspace_id=case["workspace"],
            known_parent_ids=set(case["known_parent_ids"]),
        )
    except Exception:
        return False
    return True


def test_candidate_result_differential_corpus_covers_semantics_not_schema_names() -> None:
    cases, expected = _candidate_cases()
    python_results = [_python_accepts(case) for case in cases]
    assert python_results == expected

    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const cases = {json.dumps(cases, ensure_ascii=True)}
const result = cases.map(item => {{
  try {{
    sdk.verifyResultProfile(item.value, item.workspace === null ? undefined : item.workspace, item.known_parent_ids)
    return true
  }} catch (_) {{
    return false
  }}
}})
console.log(JSON.stringify(result))
"""
    typescript_results = json.loads(_node(script).stdout)
    assert typescript_results == expected
    assert typescript_results == python_results


def _python_staging_observation(bundle: dict[str, Any]) -> dict[str, Any]:
    store = CandidateStagingStore("ws-1")
    before = list(store.visible_candidates())
    transaction = store.transaction()
    staged = transaction.stage(bundle)
    after_stage = list(store.visible_candidates())
    published = transaction.publish()
    after_publish = list(store.visible_candidates())

    failed_before = list(store.visible_candidates())
    failed = copy.deepcopy(bundle)
    failed["items"][0]["status"] = "failed"
    failed["partial"] = True
    with pytest.raises((ContractError, ContractValidationError)):
        store.stage_and_publish(failed)
    failed_after = list(store.visible_candidates())

    unknown_before = list(store.visible_candidates())
    unknown = copy.deepcopy(bundle)
    unknown["items"][0]["parent_candidate_ids"] = ["candidate-not-visible"]
    with pytest.raises((ContractError, ContractValidationError)):
        store.stage_and_publish(unknown)
    unknown_after = list(store.visible_candidates())

    skipped_before = list(store.visible_candidates())
    with pytest.raises((ContractError, ContractValidationError)):
        store.stage_and_publish(bundle, attempt_state="skipped")
    skipped_after = list(store.visible_candidates())

    return {
        "staged": {"candidate_ids": list(staged.candidate_ids), "state": staged.state},
        "published": {"candidate_ids": list(published.candidate_ids), "state": published.state},
        "before": before,
        "after_stage": after_stage,
        "after_publish": after_publish,
        "failed_unchanged": failed_after == failed_before,
        "unknown_parent_unchanged": unknown_after == unknown_before,
        "skipped_attempt_unchanged": skipped_after == skipped_before,
    }


def test_failed_staging_keeps_visible_candidate_set_unchanged_in_python_and_typescript() -> None:
    bundle = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    python_observed = _python_staging_observation(bundle)
    assert python_observed["after_stage"] == []
    assert python_observed["after_publish"] == [bundle["items"][0]]
    assert python_observed["failed_unchanged"] is True
    assert python_observed["unknown_parent_unchanged"] is True
    assert python_observed["skipped_attempt_unchanged"] is True

    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const bundle = {json.dumps(bundle, ensure_ascii=True)}
const store = new sdk.CandidateStagingStore('ws-1')
const before = store.visibleCandidates()
const transaction = store.transaction()
const staged = transaction.stage(bundle)
const after_stage = store.visibleCandidates()
const published = transaction.publish()
const after_publish = store.visibleCandidates()
const failed_before = store.visibleCandidates()
const failed = structuredClone(bundle); failed.items[0].status = 'failed'; failed.partial = true
try {{ store.stageAndPublish(failed); throw new Error('failed Candidate was published') }} catch (_) {{}}
const failed_after = store.visibleCandidates()
const unknown_before = store.visibleCandidates()
const unknown = structuredClone(bundle); unknown.items[0].parent_candidate_ids = ['candidate-not-visible']
try {{ store.stageAndPublish(unknown); throw new Error('unknown parent was published') }} catch (_) {{}}
const unknown_after = store.visibleCandidates()
const skipped_before = store.visibleCandidates()
try {{ store.stageAndPublish(bundle, 'skipped'); throw new Error('skipped Attempt was published') }} catch (_) {{}}
const skipped_after = store.visibleCandidates()
console.log(JSON.stringify({{
  staged: {{ candidate_ids: staged.candidate_ids, state: staged.state }},
  published: {{ candidate_ids: published.candidate_ids, state: published.state }},
  before,
  after_stage,
  after_publish,
  failed_unchanged: JSON.stringify(failed_after) === JSON.stringify(failed_before),
  unknown_parent_unchanged: JSON.stringify(unknown_after) === JSON.stringify(unknown_before),
  skipped_attempt_unchanged: JSON.stringify(skipped_after) === JSON.stringify(skipped_before),
}}))
"""
    typescript_observed = json.loads(_node(script).stdout)
    assert typescript_observed == python_observed
