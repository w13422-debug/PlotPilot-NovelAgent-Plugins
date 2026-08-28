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

from plotpilot_plugin_sdk.errors import ContractValidationError  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    hash_without_field,
    validate_contract,
    validate_rpc_result,
    verify_provenance_receipt,
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipt(staged_items: list[Any]) -> dict[str, Any]:
    receipt = copy.deepcopy(_json(ROOT / "contracts" / "examples" / "fixtures" / "provenance-receipt.json"))
    receipt["staged_items"] = staged_items
    receipt["receipt_hash"] = hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1")
    return receipt


def _stage_result() -> dict[str, Any]:
    return {
        "accepted": True,
        "staged_items": [
            {
                "candidate_id": "candidate-1",
                "item_id": "item-1",
                "publication_eligibility": "eligible",
                "stage_status": "created",
            },
            {
                "candidate_id": "candidate-2",
                "item_id": "item-2",
                "publication_eligibility": "review_only",
                "stage_status": "existing",
            },
        ],
        "job_event_seq": 7,
    }


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


def test_non_empty_string_array_passes_provenance_schema_and_python_verifier() -> None:
    receipt = _receipt(["item-1", "item-2"])

    assert validate_contract("provenance-receipt/v1", receipt) == []
    verify_provenance_receipt(receipt)


def test_object_mapping_is_rejected_by_provenance_schema() -> None:
    receipt = _receipt([{"item_id": "item-1"}])

    issues = validate_contract("provenance-receipt/v1", receipt)

    assert issues
    assert any(issue.path == "/staged_items/0" for issue in issues)


def test_object_mapping_remains_valid_for_host_candidate_stage_rpc_result() -> None:
    validate_rpc_result("host.candidate.stage/v1", _stage_result())


def test_duplicate_string_ids_are_rejected_by_python_verifier() -> None:
    receipt = _receipt(["item-1", "item-1"])

    with pytest.raises(ContractValidationError):
        verify_provenance_receipt(receipt)


def test_host_stage_item_ids_project_exactly_to_receipt_staged_items() -> None:
    stage_result = _stage_result()
    validate_rpc_result("host.candidate.stage/v1", stage_result)

    projected = [item["item_id"] for item in stage_result["staged_items"]]
    receipt = _receipt(projected)
    verify_provenance_receipt(receipt)

    assert receipt["staged_items"] == projected


def test_same_non_empty_string_array_passes_typescript_verifier() -> None:
    receipt = _receipt(["item-1", "item-2"])
    script = f"""
const sdk = await import('./sdk/typescript/index.ts')
const receipt = {json.dumps(receipt, ensure_ascii=True)}
await sdk.verifyProvenanceReceipt(receipt)
console.log(JSON.stringify(receipt.staged_items))
"""

    result = _node(script)

    assert json.loads(result.stdout) == ["item-1", "item-2"]
