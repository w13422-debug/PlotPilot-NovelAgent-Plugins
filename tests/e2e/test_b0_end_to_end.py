from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))


def test_minimal_b0_delivery_gate_is_executable() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "integration" / "validate_b0_delivery.py"), "--require-changes"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, f"B0 delivery gate failed\nstdout={completed.stdout}\nstderr={completed.stderr}"


def test_public_sdk_validates_fixture_surface() -> None:
    from plotpilot_plugin_sdk.verifier import validate_contract

    schema_by_fixture = {
        "capability-provider.json": "capability-provider/v1",
        "plugin-data-bundle.json": "plugin-data-bundle/v1",
        "plugin-generation.json": "plugin-generation/v1",
        "plugin-lifecycle-transition.json": "plugin-lifecycle-transition/v1",
        "plugin-manifest-code.json": "plugin-manifest/v1",
        "plugin-manifest-data.json": "plugin-manifest/v1",
        "plugin-plan.json": "plugin-plan/v1",
        "skill-manifest.json": "skill-manifest/v1",
    }
    fixture_dir = ROOT / "contracts" / "examples" / "fixtures"
    for filename, contract_id in schema_by_fixture.items():
        value = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))
        assert validate_contract(contract_id, value) == [], filename


def test_plan_rejects_legacy_compare_mode() -> None:
    from plotpilot_plugin_sdk.errors import ContractValidationError
    from plotpilot_plugin_sdk.verifier import verify_plan

    plan = json.loads((ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json").read_text(encoding="utf-8"))
    invalid = {**plan, "result_mode": "compare"}
    try:
        verify_plan(invalid)
    except ContractValidationError:
        return
    raise AssertionError("plugin-plan/v1 must reject legacy compare result_mode")
