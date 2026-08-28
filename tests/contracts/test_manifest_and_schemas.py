from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    assert_valid,
    verify_contract_inventory,
    verify_manifest,
    verify_plan,
)
from plotpilot_plugin_sdk import core_api, package, rpc, verifier  # noqa: E402
from plotpilot_plugin_sdk._workspace import WORKSPACE_ROOT  # noqa: E402


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_sdk_modules_resolve_the_current_workspace_root() -> None:
    assert WORKSPACE_ROOT == ROOT
    assert package._CASEFOLD_TABLE_PATH == ROOT / "contracts" / "unicode-casefold-v1.json"
    assert core_api.ROOT == ROOT
    assert rpc.ROOT == ROOT
    assert verifier.ROOT == ROOT


def test_accepted_contract_manifest_sha_and_inventory_are_exact() -> None:
    manifest_path = ROOT / "contracts" / "manifest-v1.json"
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == "cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024"
    manifest = _read_json(manifest_path)
    assert manifest["contract_version"] == "1.2.0"
    assert len(manifest["contract_families"]) == 20
    assert len(manifest["files"]) == 137

    listed = {record["path"] for record in manifest["files"]}
    actual = {path.relative_to(ROOT).as_posix() for path in (ROOT / "contracts").rglob("*") if path.is_file() and path != manifest_path}
    assert listed == actual
    for record in manifest["files"]:
        path = ROOT / Path(record["path"])
        raw = path.read_bytes()
        assert len(raw) == record["bytes"], path
        assert hashlib.sha256(raw).hexdigest() == record["sha256"], path


def test_every_schema_is_a_valid_draft_2020_12_schema() -> None:
    schema_dir = ROOT / "contracts" / "json-schema"
    schemas = sorted(schema_dir.glob("*.schema.json"))
    assert len(schemas) == 53
    for path in schemas:
        schema = _read_json(path)
        Draft202012Validator.check_schema(schema)
    verify_contract_inventory()


def test_public_positive_examples_are_schema_valid() -> None:
    examples = sorted((ROOT / "contracts" / "examples").rglob("*.json"))
    assert len(examples) >= 35
    for path in examples:
        value = _read_json(path)
        contract_id = value.get("schema")
        if not isinstance(contract_id, str):
            contract_id = f"{path.stem}/v1"
            assert path.name in {"rpc-error.json", "rpc-notification.json", "rpc-request.json", "rpc-success.json"}
        assert_valid(contract_id, value)


def test_frozen_manifest_and_plan_semantics_are_exposed_by_sdk() -> None:
    verify_manifest(_read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-manifest-code.json"))
    verify_manifest(_read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-manifest-data.json"))
    verify_plan(_read_json(ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json"))


def test_catalog_is_the_frozen_design_catalog_not_a_second_schema() -> None:
    catalog_path = ROOT / "catalog" / "plugin-catalog-v1.json"
    frozen_path = ROOT / "governance" / "frozen-design-v1" / "plugin-catalog-v1.json"
    catalog = _read_json(catalog_path)
    assert catalog == _read_json(frozen_path)
    assert catalog["schema"] == "novel-agent-plugin-catalog/v1"
    assert catalog["target_manifest"] == "plotpilot-plugin/v1"
    assert catalog["target_skill_manifest"] == "plotpilot-skill/v1"
