from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
TS_ROOT = SDK_ROOT / "typescript"
import sys

sys.path.insert(0, str(SDK_ROOT))

from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    validate_contract,
    verify_attempt_result,
    verify_lifecycle_transition,
    verify_result_bundle,
)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _node(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=check,
        env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )


def _diagnostic_result() -> dict[str, object]:
    result = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    return {
        **result,
        "bundle_id": "diagnostic-bundle-1",
        "bundle_type": "diagnostic",
        "contract_id": "diagnostic-bundle/v1",
        "items": [
            {
                "schema": "diagnostic-item/v1",
                "item_id": "diagnostic-item-1",
                "severity": "error",
                "code": "fixture-failure",
                "message": "deterministic failure",
                "details_asset_id": None,
                "details_hash": None,
                "source_refs": [],
                "status": "failed",
            }
        ],
        "partial": True,
    }


def test_f001_python_failed_candidate_and_attempt_profiles_fail_closed() -> None:
    candidate_bundle = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    for status in ("failed", "skipped"):
        mutated = copy.deepcopy(candidate_bundle)
        mutated["items"][0]["status"] = status
        mutated["partial"] = True
        with pytest.raises((ContractError, ContractValidationError)):
            verify_result_bundle(mutated, snapshot_workspace_id="ws-1")

    with pytest.raises((ContractError, ContractValidationError)):
        verify_attempt_result(candidate_bundle, attempt_state="failed", snapshot_workspace_id="ws-1")
    with pytest.raises((ContractError, ContractValidationError)):
        verify_attempt_result(candidate_bundle, attempt_state="skipped", snapshot_workspace_id="ws-1")
    verify_attempt_result(None, attempt_state="failed")
    verify_attempt_result(_diagnostic_result(), attempt_state="failed", snapshot_workspace_id="ws-1")
    with pytest.raises((ContractError, ContractValidationError)):
        verify_attempt_result(None, attempt_state="succeeded")


def test_f001_typescript_failed_candidate_and_attempt_profiles_match_python() -> None:
    candidate = _json(ROOT / "contracts" / "examples" / "result-bundle.json")
    diagnostic = _diagnostic_result()
    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const candidate = {json.dumps(candidate, ensure_ascii=True)}
const diagnostic = {json.dumps(diagnostic, ensure_ascii=True)}
const rejected = action => {{ try {{ action(); return false }} catch (_) {{ return true }} }}
for (const status of ['failed', 'skipped']) {{
  const item = structuredClone(candidate); item.items[0].status = status; item.partial = true
  if (!rejected(() => sdk.verifyResultProfile(item, 'ws-1'))) throw new Error('status accepted: ' + status)
}}
if (!rejected(() => sdk.verifyAttemptResult(candidate, 'failed', 'ws-1'))) throw new Error('failed Attempt accepted candidate batch')
if (!rejected(() => sdk.verifyAttemptResult(candidate, 'skipped', 'ws-1'))) throw new Error('skipped Attempt accepted candidate batch')
sdk.verifyAttemptResult(null, 'failed')
sdk.verifyAttemptResult(diagnostic, 'failed', 'ws-1')
if (!rejected(() => sdk.verifyAttemptResult(null, 'succeeded'))) throw new Error('succeeded Attempt accepted null')
console.log(JSON.stringify({{failed_candidate_rejected: true, failed_attempt_profiles: true}}))
"""
    result = _node(script)
    assert json.loads(result.stdout) == {"failed_candidate_rejected": True, "failed_attempt_profiles": True}


def test_f002_typescript_frozen_schema_inventory_and_differential_corpus() -> None:
    examples = [
        ("candidate-item/v1", _json(ROOT / "contracts" / "examples" / "candidate-item.json")),
        ("result-bundle/v1", _json(ROOT / "contracts" / "examples" / "result-bundle.json")),
        ("plotpilot-plugin/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-manifest-code.json")),
        ("plugin-plan/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json")),
        ("checkpoint/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "checkpoint.json")),
        ("stream-prefix/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "stream-prefix.json")),
        ("skill-run-receipt/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "skill-run-receipt.json")),
        ("skill-chain-result/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "skill-chain-result.json")),
        ("provenance-receipt/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "provenance-receipt.json")),
        ("sse-recovery/v1", _json(ROOT / "contracts" / "examples" / "fixtures" / "sse-recovery.json")),
    ]
    cases: list[dict[str, object]] = []
    for contract_id, value in examples:
        cases.append({"contract_id": contract_id, "value": value, "label": f"{contract_id}:valid"})
        unknown = copy.deepcopy(value)
        unknown["__unknown__"] = True
        cases.append({"contract_id": contract_id, "value": unknown, "label": f"{contract_id}:unknown"})
        required = next(iter(value))
        missing = copy.deepcopy(value)
        missing.pop(required)
        cases.append({"contract_id": contract_id, "value": missing, "label": f"{contract_id}:missing"})

    python_results = [not validate_contract(case["contract_id"], case["value"]) for case in cases]
    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const cases = {json.dumps(cases, ensure_ascii=True)}
const result = cases.map(item => sdk.schemaErrors(item.contract_id, item.value).length === 0)
console.log(JSON.stringify(result))
"""
    typescript_results = json.loads(_node(script).stdout)
    assert typescript_results == python_results

    expected_inventory = sorted(path.name for path in (ROOT / "contracts" / "json-schema").glob("*.schema.json"))
    inventory_script = """
const sdk = await import('./sdk/typescript/index.ts')
console.log(JSON.stringify(sdk.frozenSchemaInventory()))
"""
    assert json.loads(_node(inventory_script).stdout) == expected_inventory


def test_f002_typescript_receipt_checkpoint_stream_and_skill_surfaces() -> None:
    fixtures = {
        "checkpoint": _json(ROOT / "contracts" / "examples" / "fixtures" / "checkpoint.json"),
        "stream": _json(ROOT / "contracts" / "examples" / "fixtures" / "stream-prefix.json"),
        "receipt": _json(ROOT / "contracts" / "examples" / "fixtures" / "skill-run-receipt.json"),
        "chain": _json(ROOT / "contracts" / "examples" / "fixtures" / "skill-chain-result.json"),
        "provenance": _json(ROOT / "contracts" / "examples" / "fixtures" / "provenance-receipt.json"),
        "sse": _json(ROOT / "contracts" / "examples" / "fixtures" / "sse-recovery.json"),
        "settings": _json(ROOT / "contracts" / "examples" / "fixtures" / "settings-validation-receipt.json"),
    }
    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const f = {json.dumps(fixtures, ensure_ascii=True)}
await sdk.verifyCheckpoint(f.checkpoint)
await sdk.verifyStreamPrefix(f.stream)
await sdk.verifySkillReceipt(f.receipt)
await sdk.verifySkillChain(f.chain, [f.receipt])
await sdk.verifyProvenanceReceipt(f.provenance)
sdk.verifySseRecovery(f.sse)
await sdk.verifySettingsValidationReceipt(f.settings)
const rejected = async action => {{ try {{ await action(); return false }} catch (_) {{ return true }} }}
if (!await rejected(() => sdk.verifyCheckpoint({{ ...f.checkpoint, extra: true }}))) throw new Error('checkpoint unknown field accepted')
if (!await rejected(() => sdk.verifySkillReceipt({{ ...f.receipt, extra: true }}))) throw new Error('Skill receipt unknown field accepted')
console.log(JSON.stringify({{surfaces: 7, closed_negative_tests: 2}}))
"""
    assert json.loads(_node(script).stdout) == {"surfaces": 7, "closed_negative_tests": 2}


def test_f003_locked_tsc_script_and_mutation_failure() -> None:
    package = _json(TS_ROOT / "package.json")
    lock = _json(TS_ROOT / "package-lock.json")
    assert package["scripts"]["typecheck"] == "tsc --noEmit -p tsconfig.json"
    assert package["devDependencies"]["typescript"] == "5.8.3"
    assert lock["packages"]["node_modules/typescript"]["version"] == "5.8.3"
    result = subprocess.run(
        ["npm.cmd", "run", "typecheck", "--", "--pretty", "false"],
        cwd=TS_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false"},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    source_path = TS_ROOT / "types.ts"
    original = source_path.read_bytes()
    try:
        source_path.write_bytes(original + b"\nconst __nap00_typecheck_mutation: string = 1\n")
        mutated = subprocess.run(
            ["npm.cmd", "run", "typecheck", "--", "--pretty", "false"],
            cwd=TS_ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env={**os.environ, "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false"},
        )
        assert mutated.returncode != 0
        assert "TS2322" in mutated.stdout + mutated.stderr
    finally:
        source_path.write_bytes(original)


def _lifecycle_sequence() -> list[dict[str, object]]:
    selected = _json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-lifecycle-transition.json")
    selected.update(
        {
            "base_generation_id": "generation-current",
            "base_lkg_generation_id": "generation-lkg",
            "target_generation_id": "generation-new",
            "package_store_status": "absent",
            "target_settings_revision_ids": [{"plugin_id": "com.plotpilot.demo", "settings_revision_id": "settings-1"}],
        }
    )
    states: list[dict[str, object]] = [selected]
    for state, package_store_status, extra in (
        ("staged", "staged", {}),
        ("package_published", "published", {}),
        ("env_prepared", "published", {}),
        ("shadow_prepared", "published", {"shadow_data_generation_id": "data-generation-1"}),
        ("migrated", "published", {}),
        ("settings_validated", "published", {}),
        ("qualified", "published", {"qualification_id": "qualification-1"}),
        ("pending_apply", "published", {}),
        ("current_committed", "published", {}),
        ("rollback_armed", "published", {"rollback_token": "rollback-1"}),
    ):
        current = copy.deepcopy(states[-1])
        current.update({"state": state, "package_store_status": package_store_status, **extra})
        states.append(current)
    rolled_back = copy.deepcopy(states[-1])
    rolled_back.update({"state": "rolled_back", "rollback_attempt": 1})
    states.append(rolled_back)
    safe_mode = copy.deepcopy(rolled_back)
    safe_mode["state"] = "safe_mode"
    states.append(safe_mode)
    return states


def test_f005_lifecycle_legal_edges_exact_lkg_and_identity_drift_matrix() -> None:
    sequence = _lifecycle_sequence()
    verify_lifecycle_transition(sequence[0])
    for previous, current in zip(sequence, sequence[1:]):
        verify_lifecycle_transition(current, previous=previous)

    # Normal LKG promotion is a separate legal path and never carries a
    # rollback attempt/token.
    promoted = _lifecycle_sequence()[:10]
    promoted[-1]["state"] = "current_committed"
    promoted[-1]["rollback_token"] = None
    promoted[-1]["rollback_attempt"] = 0
    lkg_pending = copy.deepcopy(promoted[-1])
    lkg_pending["state"] = "lkg_pending"
    lkg_promoted = copy.deepcopy(lkg_pending)
    lkg_promoted["state"] = "lkg_promoted"
    verify_lifecycle_transition(lkg_pending, previous=promoted[-1])
    verify_lifecycle_transition(lkg_promoted, previous=lkg_pending)

    armed = sequence[-3]
    for field, value in (
        ("base_generation_id", "generation-other"),
        ("base_lkg_generation_id", "generation-other"),
        ("target_generation_id", "generation-other"),
        ("target_settings_revision_ids", [{"plugin_id": "com.plotpilot.demo", "settings_revision_id": "settings-other"}]),
        ("rollback_token", "rollback-other"),
    ):
        mutated = copy.deepcopy(armed)
        mutated[field] = value
        with pytest.raises((ContractError, ContractValidationError)):
            verify_lifecycle_transition(mutated, previous=armed)

    wrong_lkg = copy.deepcopy(sequence[-2])
    wrong_lkg["base_lkg_generation_id"] = "generation-other"
    with pytest.raises((ContractError, ContractValidationError)):
        verify_lifecycle_transition(wrong_lkg, previous=armed)
    rewritten_target = copy.deepcopy(sequence[-2])
    rewritten_target["target_generation_id"] = rewritten_target["base_lkg_generation_id"]
    with pytest.raises((ContractError, ContractValidationError)):
        verify_lifecycle_transition(rewritten_target, previous=armed)
    with pytest.raises((ContractError, ContractValidationError)):
        verify_lifecycle_transition(sequence[-2], previous=sequence[-2])

    package_backwards = copy.deepcopy(sequence[3])
    package_backwards["state"] = "env_prepared"
    package_backwards["package_store_status"] = "staged"
    with pytest.raises((ContractError, ContractValidationError)):
        verify_lifecycle_transition(package_backwards, previous=sequence[2])

    time_backwards = copy.deepcopy(sequence[1])
    time_backwards["updated_at"] = "2026-08-25T00:00:00Z"
    with pytest.raises((ContractError, ContractValidationError)):
        verify_lifecycle_transition(time_backwards, previous=sequence[0])


def test_f005_typescript_lifecycle_edges_and_drift_matrix_match_python() -> None:
    sequence = _lifecycle_sequence()
    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const sequence = {json.dumps(sequence, ensure_ascii=True)}
sdk.verifyLifecycleTransition(sequence[0])
for (let index = 1; index < sequence.length; index += 1) sdk.verifyLifecycleTransition(sequence[index], sequence[index - 1])
const rejected = action => {{ try {{ action(); return false }} catch (_) {{ return true }} }}
const armed = sequence[sequence.length - 3]
for (const [field, value] of [['base_generation_id', 'generation-other'], ['base_lkg_generation_id', 'generation-other'], ['target_generation_id', 'generation-other'], ['target_settings_revision_ids', [{{ plugin_id: 'com.plotpilot.demo', settings_revision_id: 'settings-other' }}]], ['rollback_token', 'rollback-other']]) {{
  const mutated = structuredClone(armed); mutated[field] = value
  if (!rejected(() => sdk.verifyLifecycleTransition(mutated, armed))) throw new Error('drift accepted: ' + field)
}}
const wrong = structuredClone(sequence[sequence.length - 2]); wrong.base_lkg_generation_id = 'generation-other'
if (!rejected(() => sdk.verifyLifecycleTransition(wrong, armed))) throw new Error('wrong LKG accepted')
const rewrittenTarget = structuredClone(sequence[sequence.length - 2]); rewrittenTarget.target_generation_id = rewrittenTarget.base_lkg_generation_id
if (!rejected(() => sdk.verifyLifecycleTransition(rewrittenTarget, armed))) throw new Error('rollback target rewrite accepted')
if (!rejected(() => sdk.verifyLifecycleTransition(sequence[sequence.length - 2], sequence[sequence.length - 2]))) throw new Error('duplicate rollback accepted')
console.log(JSON.stringify({{legal_edges: sequence.length - 1, drift_rejected: 8}}))
"""
    assert json.loads(_node(script).stdout) == {"legal_edges": 12, "drift_rejected": 8}
