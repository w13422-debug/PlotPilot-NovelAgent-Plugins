"""Rebuild and verify plot-structure-template/v1 with stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent / "v1"
PLUGIN_ID = "com.plotpilot.novelagent.plot-structure-template"
CODE_PLUGIN_ID = "com.plotpilot.novelagent.narrative-analysis"
FORMAT_ID = "plot-structure-template/v1"
VERSION = "1.0.0"
PAYLOAD = (
    "data/templates.json", "fixtures/interpreter-roundtrip.json", "plugin.json",
    "schemas/interpreter-roundtrip-v1.schema.json", "schemas/plot-structure-template-v1.schema.json",
)
INTERPRETERS = (
    ("com.plotpilot.novelagent.narrative-analysis", "analysis.narrative.unit.extract/v1"),
    ("com.plotpilot.novelagent.narrative-analysis", "analysis.narrative.plan.compile/v1"),
    ("com.plotpilot.novelagent.narrative-analysis", "analysis.narrative.synthesize/v1"),
    ("com.plotpilot.novelagent.asset-derivation", "asset.template.derive/v1"),
    ("com.plotpilot.novelagent.writing-context", "writing.context.assemble/v1"),
    ("com.plotpilot.novelagent.chapter-workflow", "writing.project.outline/v1"),
    ("com.plotpilot.novelagent.chapter-workflow", "writing.volume.outline/v1"),
    ("com.plotpilot.novelagent.chapter-workflow", "writing.chapter.outline/v1"),
)


def _sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def _json(value: object) -> bytes: return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
def _canonical(value: object) -> bytes: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[path])}  {path}\n" for path in sorted(files, key=lambda item: item.encode())).encode()


def _release(package_hash: str) -> str:
    return _sha(f"plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n".encode("ascii"))


def _load(path: str) -> dict[str, Any]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ValueError(f"{path} must contain an object")
    return value


def _validate() -> None:
    value = _load("data/templates.json")
    if set(value) != {"schema", "format_id", "template_set_id", "version", "templates", "interpreter_mappings", "mergeable"}:
        raise ValueError("template package root is not closed")
    if value["schema"] != FORMAT_ID or value["format_id"] != FORMAT_ID or value["version"] != VERSION or value["mergeable"] is not False:
        raise ValueError("template identity/merge policy drift")
    actual = {(item["plugin_id"], item["capability_id"], item["direction"]) for item in value["interpreter_mappings"]}
    expected = {(plugin, capability, direction) for plugin, capability in INTERPRETERS for direction in ("format_to_capability", "capability_to_format")}
    if actual != expected or len(value["interpreter_mappings"]) != len(expected):
        raise ValueError("catalog interpreter mapping is not bidirectionally closed")
    for template in value["templates"]:
        if template["levels"] != ["book", "volume", "chapter", "plot_unit"] or template["constraints"]["authority"] != "template_only" or template["constraints"]["requires_evidence_closure"] is not True:
            raise ValueError("immutable hierarchy/template authority drift")
        orders = [item["order"] for item in template["beats"]]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("template beats are not deterministic")
    fixture = _load("fixtures/interpreter-roundtrip.json")
    if [(item["plugin_id"], item["capability_id"]) for item in fixture["interpreters"]] != list(INTERPRETERS) or fixture["mergeable"] is not False:
        raise ValueError("interpreter round-trip fixture drift")


def _generated() -> dict[str, bytes]:
    _validate()
    files = {path: (ROOT / path).read_bytes() for path in PAYLOAD}
    manifest = _manifest(files)
    package_hash = _sha(b"plotpilot-package/v1\n" + manifest)
    release_id = _release(package_hash)
    data_root = ROOT / "data/templates.json"
    bundle_body = {
        "schema": "plugin-data-bundle/v1", "bundle_id": "plot-structure-template-v1-bundle",
        "data_plugin_id": PLUGIN_ID, "data_release_id": release_id, "package_hash": package_hash,
        "format_id": FORMAT_ID, "root_path": "data/templates.json",
        "files": [{"path": "data/templates.json", "asset_id": "asset-plot-structure-template-v1",
                   "sha256": _sha(data_root.read_bytes()), "mime": "application/json", "size": len(data_root.read_bytes())}],
    }
    bundle = {**bundle_body, "bundle_hash": _sha(b"plugin-data-bundle/v1\n" + _canonical(bundle_body))}
    identity = {
        "schema": "plot-structure-template-package-identity/v1", "package_kind": "data",
        "plugin_id": PLUGIN_ID, "format_id": FORMAT_ID, "version": VERSION,
        "package_hash": package_hash, "release_id": release_id,
        "separate_from_code_plugin_id": CODE_PLUGIN_ID,
    }
    expected = {
        "schema": "plot-structure-template-expected/v1", "package_kind": "data",
        "plugin_id": PLUGIN_ID, "format_id": FORMAT_ID, "version": VERSION,
        "manifest_path": "plugin.json", "root_path": "data/templates.json",
        "package_files": list(PAYLOAD), "files_sha256": manifest.decode(),
        "fixture_hashes": {path: _sha((ROOT / path).read_bytes()) for path in (
            "fixtures/interpreter-roundtrip.json", "schemas/plot-structure-template-v1.schema.json",
            "schemas/interpreter-roundtrip-v1.schema.json", "data/templates.json")},
        "package_hash": package_hash, "release_id": release_id, "bundle_hash": bundle["bundle_hash"],
        "identity_path": "identity.json",
        "identity_domains": {"data_or_code": "plotpilot-release/v1", "skill": "plotpilot-skill-release/v1", "code_plugin_id": CODE_PLUGIN_ID},
    }
    return {"files.sha256": manifest, "identity.json": _json(identity), "expected.json": _json(expected), "fixtures/plugin-data-bundle.json": _json(bundle)}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--rebuild", action="store_true"); args = parser.parse_args()
    generated = _generated()
    if args.rebuild:
        for name, raw in generated.items(): (ROOT / name).write_bytes(raw)
    for name, raw in generated.items():
        if not (ROOT / name).is_file() or (ROOT / name).read_bytes() != raw: raise SystemExit(f"DRIFT {name}")
    expected = json.loads(generated["expected.json"])
    print(json.dumps({"format_id": FORMAT_ID, "package_hash": expected["package_hash"], "release_id": expected["release_id"], "status": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
