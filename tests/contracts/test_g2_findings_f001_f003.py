import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
import sys

sys.path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.canonical import canonical_bytes, sha256_hex  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    build_claim_input_asset,
    request_key,
    snapshot_hash,
    verify_attempt_result,
    verify_claim_input,
    verify_claim_input_asset,
    verify_claim_input_structure,
    verify_plan,
    verify_result_bundle,
    verify_snapshot,
)


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        env={**os.environ, "NODE_NO_WARNINGS": "1", "PYTHONUTF8": "1"},
    )


def _accepts(action) -> bool:
    try:
        action()
    except Exception:
        return False
    return True


def _skill_ref(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": "chain-1",
        "asset_id": None,
        "asset_hash": None,
        "result_bundle_id": None,
        "result_item_id": None,
        "stream_id": None,
        "acked_prefix_hash": None,
    }
    value.update(overrides)
    return value


def test_f001_result_context_fences_and_plan_context_matrix_match_python_and_typescript() -> None:
    base_bundle = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    assert isinstance(base_bundle, dict)
    bundle = base_bundle
    cases: list[dict[str, object]] = [
        {
            "label": "valid",
            "bundle": bundle,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": True,
        },
    ]

    bundle_ref = copy.deepcopy(bundle)
    bundle_ref["skill_chain_result_refs"] = [
        _skill_ref(result_bundle_id="bundle-golden", result_item_id="candidate-item-1")
    ]
    cases.append(
        {
            "label": "bundle-and-item-backed-skill-ref",
            "bundle": bundle_ref,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": True,
        }
    )

    wrong_snapshot = copy.deepcopy(bundle)
    cases.append(
        {
            "label": "wrong-snapshot-hash",
            "bundle": wrong_snapshot,
            "workspace": "ws-1",
            "snapshot_hash": "0" * 64,
            "known_parent_ids": [],
            "accepted": False,
        }
    )

    foreign_bundle = copy.deepcopy(bundle)
    foreign_bundle["skill_chain_result_refs"] = [
        _skill_ref(result_bundle_id="foreign-bundle", result_item_id="candidate-item-1")
    ]
    cases.append(
        {
            "label": "foreign-bundle-skill-ref",
            "bundle": foreign_bundle,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": False,
        }
    )

    foreign_item = copy.deepcopy(bundle)
    foreign_item["skill_chain_result_refs"] = [
        _skill_ref(result_bundle_id="bundle-golden", result_item_id="foreign-item")
    ]
    cases.append(
        {
            "label": "foreign-item-skill-ref",
            "bundle": foreign_item,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": False,
        }
    )

    stream_backed = copy.deepcopy(bundle)
    stream_backed["skill_chain_result_refs"] = [
        _skill_ref(stream_id="stream-1", acked_prefix_hash="a" * 64)
    ]
    cases.append(
        {
            "label": "stream-backed-skill-ref",
            "bundle": stream_backed,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": False,
        }
    )

    duplicate_incomplete = copy.deepcopy(bundle)
    second = copy.deepcopy(duplicate_incomplete["items"][0])
    second["item_id"] = "candidate-item-2"
    second["status"] = "partial"
    duplicate_incomplete["items"][0]["status"] = "partial"
    duplicate_incomplete["items"][0]["item_kind"] = "incomplete_stream"
    duplicate_incomplete["items"][0]["mutation"]["mode"] = "replace"
    duplicate_incomplete["items"][0]["mutation"]["payload_schema"] = "core/document-text/v1"
    second["item_kind"] = "incomplete_stream"
    second["mutation"]["mode"] = "replace"
    second["mutation"]["payload_schema"] = "core/document-text/v1"
    duplicate_incomplete["items"].append(second)
    duplicate_incomplete["partial"] = True
    cases.append(
        {
            "label": "duplicate-incomplete-stream-target",
            "bundle": duplicate_incomplete,
            "workspace": "ws-1",
            "snapshot_hash": bundle["input_snapshot_hash"],
            "known_parent_ids": [],
            "accepted": False,
        }
    )

    python_results = []
    for case in cases:
        python_results.append(
            _accepts(
                lambda case=case: verify_result_bundle(
                    case["bundle"],
                    snapshot_workspace_id=case["workspace"],
                    snapshot_hash_value=case["snapshot_hash"],
                    known_parent_ids=set(case["known_parent_ids"]),
                )
            )
        )
    assert python_results == [case["accepted"] for case in cases]

    ts_script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const cases = {json.dumps(cases, ensure_ascii=True)}
const result = cases.map(item => {{
  try {{
    sdk.verifyResultProfile(item.bundle, {{ snapshotWorkspaceId: item.workspace, snapshotHashValue: item.snapshot_hash, knownParentIds: item.known_parent_ids }})
    return true
  }} catch (_) {{ return false }}
}})
const attempt = [true, false].map((expected, index) => {{
  try {{
    sdk.verifyAttemptResult(cases[0].bundle, 'succeeded', {{ snapshotWorkspaceId: 'ws-1', snapshotHashValue: index === 0 ? cases[0].snapshot_hash : '0'.repeat(64), knownParentIds: [] }})
    return true
  }} catch (_) {{ return false }}
}})
console.log(JSON.stringify({{ result, attempt }}))
"""
    observed = json.loads(_node(ts_script).stdout)
    assert observed["result"] == python_results
    assert observed["attempt"] == [True, False]

    catalog = _json(ROOT / "catalog" / "plugin-catalog-v1.json")
    interpreter = {
        "interpreter-1": {
            "plugin_id": "com.plotpilot.novelagent.chapter-workflow",
            "capability_id": "writing.chapter.draft/v1",
        }
    }
    base_plan = _json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json")
    populated_plan = copy.deepcopy(base_plan)
    populated_plan["data_bindings"] = [
        {
            "data_binding_id": "data-binding-1",
            "data_plugin_id": "com.plotpilot.data.style",
            "release_requirement": "1.0.0",
            "format_id": "style-pack/v1",
            "interpreter_binding_id": "interpreter-1",
            "order": 10,
            "enabled": True,
            "parameters_asset_id": None,
        }
    ]
    matrix: list[dict[str, object]] = []
    for label, plan in (("empty", base_plan), ("populated", populated_plan)):
        for catalog_present, interpreter_present in ((False, False), (True, False), (False, True), (True, True)):
            matrix.append(
                {
                    "label": f"{label}:{catalog_present}:{interpreter_present}",
                    "plan": plan,
                    "catalog": catalog if catalog_present else None,
                    "interpreter": interpreter if interpreter_present else None,
                    "accepted": (not catalog_present and not interpreter_present)
                    or (catalog_present and interpreter_present),
                }
            )
    python_plan_results = []
    for case in matrix:
        python_plan_results.append(
            _accepts(
                lambda case=case: verify_plan(
                    case["plan"],
                    catalog=case["catalog"],
                    interpreter_bindings=case["interpreter"],
                )
            )
        )
    assert python_plan_results == [case["accepted"] for case in matrix]
    ts_plan_script = """
const { readFileSync } = await import('node:fs')
const sdk = await import('./sdk/typescript/index.ts')
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const catalog = readJson('catalog/plugin-catalog-v1.json')
const basePlan = readJson('contracts/examples/fixtures/plugin-plan.json')
const interpreter = { 'interpreter-1': { plugin_id: 'com.plotpilot.novelagent.chapter-workflow', capability_id: 'writing.chapter.draft/v1' } }
const populatedPlan = { ...basePlan, data_bindings: [{ data_binding_id: 'data-binding-1', data_plugin_id: 'com.plotpilot.data.style', release_requirement: '1.0.0', format_id: 'style-pack/v1', interpreter_binding_id: 'interpreter-1', order: 10, enabled: true, parameters_asset_id: null }] }
const cases = [
  [basePlan, undefined, undefined], [basePlan, undefined, catalog], [basePlan, interpreter, undefined], [basePlan, interpreter, catalog],
  [populatedPlan, undefined, undefined], [populatedPlan, undefined, catalog], [populatedPlan, interpreter, undefined], [populatedPlan, interpreter, catalog],
]
const result = cases.map(([plan, bindings, suppliedCatalog]) => {
  try { sdk.verifyPlan(plan, bindings, suppliedCatalog); return true }
  catch (_) { return false }
})
console.log(JSON.stringify(result))
    """
    assert json.loads(_node(ts_plan_script).stdout) == python_plan_results

    base_snapshot = _json(ROOT / "contracts" / "golden" / "run-snapshot" / "snapshot.json")
    empty_snapshot = copy.deepcopy(base_snapshot)
    empty_snapshot["data_bindings"] = []
    empty_snapshot["request_key"] = request_key(empty_snapshot)
    empty_snapshot["snapshot_hash"] = snapshot_hash(empty_snapshot)
    populated_snapshot = copy.deepcopy(base_snapshot)
    populated_snapshot["data_bindings"] = [
        {
            "bundle_asset_id": "asset-data-a",
            "bundle_hash": "8" * 64,
            "data_plugin_id": "com.plotpilot.data.style",
            "data_release_id": "data-rel-style",
            "format_id": "style-pack/v1",
            "interpreter_binding_id": "interpreter-1",
            "order": 10,
        }
    ]
    populated_snapshot["request_key"] = request_key(populated_snapshot)
    populated_snapshot["snapshot_hash"] = snapshot_hash(populated_snapshot)
    snapshot_matrix: list[dict[str, object]] = []
    for label, snapshot_value in (("empty", empty_snapshot), ("populated", populated_snapshot)):
        for catalog_present, interpreter_present in ((False, False), (True, False), (False, True), (True, True)):
            snapshot_matrix.append(
                {
                    "label": f"snapshot-{label}:{catalog_present}:{interpreter_present}",
                    "snapshot": snapshot_value,
                    "catalog": catalog if catalog_present else None,
                    "interpreter": interpreter if interpreter_present else None,
                    "accepted": (not catalog_present and not interpreter_present)
                    or (catalog_present and interpreter_present),
                }
            )
    python_snapshot_results = []
    for case in snapshot_matrix:
        python_snapshot_results.append(
            _accepts(
                lambda case=case: verify_snapshot(
                    case["snapshot"],
                    catalog=case["catalog"],
                    interpreter_bindings=case["interpreter"],
                )
            )
        )
    assert python_snapshot_results == [case["accepted"] for case in snapshot_matrix]
    ts_snapshot_script = """
const { readFileSync } = await import('node:fs')
const sdk = await import('./sdk/typescript/index.ts')
const readJson = path => JSON.parse(readFileSync(path, 'utf8'))
const catalog = readJson('catalog/plugin-catalog-v1.json')
const baseSnapshot = readJson('contracts/golden/run-snapshot/snapshot.json')
const interpreter = { 'interpreter-1': { plugin_id: 'com.plotpilot.novelagent.chapter-workflow', capability_id: 'writing.chapter.draft/v1' } }
const dataBinding = { bundle_asset_id: 'asset-data-a', bundle_hash: '8'.repeat(64), data_plugin_id: 'com.plotpilot.data.style', data_release_id: 'data-rel-style', format_id: 'style-pack/v1', interpreter_binding_id: 'interpreter-1', order: 10 }
const rehash = async snapshot => {
  snapshot.request_key = await sdk.requestKey(snapshot)
  const unsigned = { ...snapshot }
  delete unsigned.snapshot_hash
  snapshot.snapshot_hash = await sdk.hashJcs('run-snapshot/v1', unsigned)
  return snapshot
}
const emptySnapshot = await rehash({ ...structuredClone(baseSnapshot), data_bindings: [] })
const populatedSnapshot = await rehash({ ...structuredClone(baseSnapshot), data_bindings: [dataBinding] })
const cases = [
  [emptySnapshot, undefined, undefined], [emptySnapshot, undefined, catalog], [emptySnapshot, interpreter, undefined], [emptySnapshot, interpreter, catalog],
  [populatedSnapshot, undefined, undefined], [populatedSnapshot, undefined, catalog], [populatedSnapshot, interpreter, undefined], [populatedSnapshot, interpreter, catalog],
]
const result = await Promise.all(cases.map(async ([snapshot, bindings, suppliedCatalog]) => {
  try { await sdk.verifySnapshot(snapshot, bindings, suppliedCatalog); return true }
  catch (_) { return false }
}))
console.log(JSON.stringify(result))
"""
    assert json.loads(_node(ts_snapshot_script).stdout) == python_snapshot_results


def test_f002_canonical_unicode_vectors_match_python_and_typescript_bytes_and_hashes() -> None:
    vectors = _json(ROOT / "tests" / "contracts" / "canonical_unicode_vectors.json")
    assert isinstance(vectors, list)
    python_results: list[dict[str, object]] = []
    for vector in vectors:
        value = vector["value"]
        try:
            raw = canonical_bytes(value)
            canonical_ok = True
            result: dict[str, object] = {
                "label": vector["label"],
                "accepted": canonical_ok,
                "canonical_hex": raw.hex(),
                "sha256": sha256_hex(raw),
                "direct_utf8_rejected": None,
            }
        except Exception:
            result = {
                "label": vector["label"],
                "accepted": False,
                "canonical_hex": None,
                "sha256": None,
                "direct_utf8_rejected": None,
            }
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
                result["direct_utf8_rejected"] = False
            except UnicodeEncodeError:
                result["direct_utf8_rejected"] = True
        python_results.append(result)
    assert [item["accepted"] for item in python_results] == [item["accepted"] for item in vectors]

    ts_script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const vectors = {json.dumps(vectors, ensure_ascii=True)}
const result = await Promise.all(vectors.map(async item => {{
  let accepted = false
  let canonicalHex = null
  let digest = null
  try {{
    const canonical = sdk.canonicalJson(item.value)
    const raw = sdk.utf8(canonical)
    accepted = true
    canonicalHex = Array.from(raw, byte => byte.toString(16).padStart(2, '0')).join('')
    digest = await sdk.sha256Hex(raw)
  }} catch (_) {{}}
  let directUtf8Rejected = null
  if (typeof item.value === 'string') {{
    try {{ sdk.utf8(item.value); directUtf8Rejected = false }} catch (_) {{ directUtf8Rejected = true }}
  }}
  return {{ label: item.label, accepted, canonical_hex: canonicalHex, sha256: digest, direct_utf8_rejected: directUtf8Rejected }}
}}))
console.log(JSON.stringify(result))
"""
    assert json.loads(_node(ts_script).stdout) == python_results


def _claim_fixture() -> tuple[dict[str, object], dict[str, dict[str, object]], bytes, dict[str, object]]:
    canonical_text = "ABC"
    span = {
        "schema": "evidence-span/v1",
        "workspace_id": "ws-1",
        "document_id": "doc-1",
        "revision_id": "rev-1",
        "node_id": "node-1",
        "start_codepoint": 1,
        "end_codepoint": 2,
        "quote": "B",
        "quote_hash": hashlib.sha256(b"B").hexdigest(),
        "canonical_text_hash": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
    }
    atom = {
        "ordinal": 0,
        "atom_id": "atom-1",
        "payload_hash": "1" * 64,
        "acceptance_ordinal": 1,
        "evidence_spans": [span],
    }
    claim = {"schema": "claim-input/v1", "source_revision_id": "rev-1", "ordered_atoms": [atom]}
    valid = {
        "atom_id": "atom-1",
        "payload_hash": "1" * 64,
        "acceptance_ordinal": 1,
        "current": True,
        "accepted": True,
        "revision_id": "rev-1",
    }
    raw, asset_hash = build_claim_input_asset("rev-1", [atom], accepted_atoms={"atom-1": valid})
    snapshot = _json(ROOT / "contracts" / "golden" / "run-snapshot" / "snapshot.json")
    snapshot["asset_hashes"] = [
        {**item, "sha256": asset_hash if item["asset_id"] == "asset-params" else item["sha256"]}
        for item in snapshot["asset_hashes"]
    ]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    return claim, {"atom-1": valid}, raw, snapshot


def test_f003_claim_sealing_requires_complete_authority_and_snapshot_binding_matches_python_and_typescript() -> None:
    claim, valid_authority, raw, snapshot = _claim_fixture()
    verify_claim_input_structure(claim)
    valid_record = valid_authority["atom-1"]

    def record(**updates: object) -> dict[str, dict[str, object]]:
        value = copy.deepcopy(valid_record)
        value.update(updates)
        return {"atom-1": value}

    missing_current = copy.deepcopy(valid_record)
    missing_current.pop("current")
    missing_accepted = copy.deepcopy(valid_record)
    missing_accepted.pop("accepted")
    missing_payload = copy.deepcopy(valid_record)
    missing_payload.pop("payload_hash")
    missing_ordinal = copy.deepcopy(valid_record)
    missing_ordinal.pop("acceptance_ordinal")
    authority_cases: dict[str, object] = {
        "absent": None,
        "unknown-atom": {"atom-other": valid_record},
        "missing-current": {"atom-1": missing_current},
        "missing-accepted": {"atom-1": missing_accepted},
        "missing-payload": {"atom-1": missing_payload},
        "missing-ordinal": {"atom-1": missing_ordinal},
        "current-false": record(current=False),
        "accepted-false": record(accepted=False),
        "wrong-atom-id": record(atom_id="atom-other"),
        "stale-payload": record(payload_hash="0" * 64),
        "stale-ordinal": record(acceptance_ordinal=2),
        "cross-revision": record(revision_id="rev-2"),
        "valid": valid_authority,
    }
    python_decisions: dict[str, bool] = {}
    for label, authority in authority_cases.items():
        python_decisions[label] = _accepts(
            lambda authority=authority: verify_claim_input(
                claim,
                expected_source_revision_id="rev-1",
                accepted_atoms=authority,
            )
        )
    assert python_decisions["valid"] is True
    assert all(not value for key, value in python_decisions.items() if key != "valid")
    assert _accepts(lambda: build_claim_input_asset("rev-1", [claim["ordered_atoms"][0]], accepted_atoms=valid_authority))
    assert not _accepts(lambda: build_claim_input_asset("rev-1", [claim["ordered_atoms"][0]]))
    assert _accepts(
        lambda: verify_claim_input_asset(
            raw,
            snapshot,
            expected_source_revision_id="rev-1",
            accepted_atoms=valid_authority,
            asset_id="asset-params",
        )
    )
    assert not _accepts(lambda: verify_claim_input_asset(raw, snapshot))
    assert not _accepts(
        lambda: verify_claim_input_asset(raw, snapshot, accepted_atoms=valid_authority, asset_id="wrong-asset")
    )

    ts_script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const claim = {json.dumps(claim, ensure_ascii=True)}
const authorityCases = {json.dumps(authority_cases, ensure_ascii=True)}
const snapshot = {json.dumps(snapshot, ensure_ascii=True)}
const raw = new Uint8Array({json.dumps(list(raw))})
const rejected = async action => {{ try {{ await action(); return false }} catch (_) {{ return true }} }}
await sdk.verifyClaimInputStructure(claim, {{ expectedSourceRevisionId: 'rev-1' }})
const decisions = {{}}
for (const [label, authority] of Object.entries(authorityCases)) {{
  decisions[label] = await rejected(() => sdk.verifyClaimInput(claim, {{ expectedSourceRevisionId: 'rev-1', acceptedAtoms: authority }})) === false
}}
const built = await sdk.buildClaimInputAsset('rev-1', claim.ordered_atoms, {{ acceptedAtoms: authorityCases.valid, expectedSourceRevisionId: 'rev-1' }})
const buildAbsentRejected = await rejected(() => sdk.buildClaimInputAsset('rev-1', claim.ordered_atoms))
await sdk.verifyClaimInputAsset(raw, snapshot, {{ assetId: 'asset-params', expectedSourceRevisionId: 'rev-1', acceptedAtoms: authorityCases.valid }})
const assetAbsentRejected = await rejected(() => sdk.verifyClaimInputAsset(raw, snapshot))
const assetWrongIdRejected = await rejected(() => sdk.verifyClaimInputAsset(raw, snapshot, {{ assetId: 'wrong-asset', acceptedAtoms: authorityCases.valid }}))
console.log(JSON.stringify({{ decisions, built_asset_hash: built.assetHash, built_bytes: Array.from(built.bytes), buildAbsentRejected, assetAbsentRejected, assetWrongIdRejected }}))
"""
    observed = json.loads(_node(ts_script).stdout)
    assert observed["decisions"] == python_decisions
    assert observed["built_asset_hash"] == sha256_hex(raw)
    assert observed["built_bytes"] == list(raw)
    assert observed["buildAbsentRejected"] is True
    assert observed["assetAbsentRejected"] is True
    assert observed["assetWrongIdRejected"] is True
