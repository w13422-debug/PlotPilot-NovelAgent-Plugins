"""Rebuild and verify the five immutable outline Skill packages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"
FORMAT_ID = "plot-structure-template/v1"
CODE_PLUGIN_ID = "com.plotpilot.novelagent.narrative-analysis"
DATA_PLUGIN_ID = "com.plotpilot.novelagent.plot-structure-template"
PAYLOAD = ("method.json", "method.schema.json", "prompt.txt", "skill.json")
SKILLS = {
    "narrative-unit": ("com.plotpilot.skill.donor.narrative-unit", "analysis", ["extract"], ["analysis.narrative.unit.extract/v1"]),
    "book": ("com.plotpilot.skill.outline.book", "outline", ["plan"], ["analysis.narrative.plan.compile/v1"]),
    "volume": ("com.plotpilot.skill.outline.volume", "outline", ["plan"], ["analysis.narrative.plan.compile/v1"]),
    "chapter": ("com.plotpilot.skill.outline.chapter", "outline", ["plan"], ["analysis.narrative.plan.compile/v1"]),
    "plot-unit": ("com.plotpilot.skill.outline.plot-unit", "outline", ["plan"], ["analysis.narrative.plan.compile/v1"]),
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[path])}  {path}\n" for path in sorted(files, key=lambda item: item.encode())).encode()


def _release(skill_id: str, package_hash: str) -> str:
    return _sha(f"plotpilot-skill-release/v1\n{skill_id}\n{VERSION}\n{package_hash}\n".encode("ascii"))


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one object")
    return value


def _generated(name: str, spec: tuple[str, str, list[str], list[str]]) -> dict[str, bytes]:
    skill_id, stage, actions, capabilities = spec
    package = ROOT / name
    manifest = _object(package / "skill.json")
    if set(manifest) != {"schema", "skill_id", "version", "display_name", "stage", "actions"}:
        raise ValueError(f"{name} Skill manifest is not closed")
    if manifest["schema"] != "plotpilot-skill/v1" or manifest["skill_id"] != skill_id or manifest["version"] != VERSION or manifest["stage"] != stage or manifest["actions"] != actions:
        raise ValueError(f"{name} Skill manifest identity drift")
    method = _object(package / "method.json")
    expected_keys = {"schema", "skill_id", "input_format_id", "capability_ids", "result_contract", "rules", "authority", "immutable"}
    if set(method) != expected_keys or method["schema"] != "outline-skill-method/v1" or method["skill_id"] != skill_id or method["input_format_id"] != FORMAT_ID or method["capability_ids"] != capabilities or method["result_contract"] != "candidate-batch/v1" or method["authority"] != "candidate_only" or method["immutable"] is not True:
        raise ValueError(f"{name} immutable method contract drift")
    if not (package / "prompt.txt").read_text(encoding="utf-8").strip():
        raise ValueError(f"{name} prompt is empty")
    files = {path: (package / path).read_bytes() for path in PAYLOAD}
    files_sha256 = _manifest(files)
    package_hash = _sha(b"plotpilot-skill-package/v1\n" + files_sha256)
    release_id = _release(skill_id, package_hash)
    identity = {
        "schema": "outline-skill-package-identity/v1", "package_kind": "skill",
        "skill_id": skill_id, "version": VERSION, "skill_package_hash": package_hash,
        "skill_release_id": release_id, "separate_from_data_plugin_id": DATA_PLUGIN_ID,
        "separate_from_code_plugin_id": CODE_PLUGIN_ID,
    }
    expected = {
        "schema": "outline-skill-package-expected/v1", "package_kind": "skill",
        "skill_id": skill_id, "version": VERSION, "manifest_path": "skill.json",
        "method_path": "method.json", "package_files": list(PAYLOAD),
        "files_sha256": files_sha256.decode(),
        "fixture_hashes": {path: _sha(files[path]) for path in ("method.json", "prompt.txt", "method.schema.json")},
        "skill_package_hash": package_hash, "skill_release_id": release_id,
        "identity_path": "identity.json",
        "identity_domains": {"skill": "plotpilot-skill-release/v1", "data_or_code": "plotpilot-release/v1", "code_plugin_id": CODE_PLUGIN_ID},
    }
    return {"files.sha256": files_sha256, "identity.json": _json(identity), "expected.json": _json(expected)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    shared_schema = (ROOT / "narrative-unit" / "method.schema.json").read_bytes()
    result: dict[str, object] = {"status": "PASS", "skills": {}}
    seen: set[str] = set()
    for name, spec in SKILLS.items():
        package = ROOT / name
        schema_path = package / "method.schema.json"
        if args.rebuild:
            schema_path.write_bytes(shared_schema)
        generated = _generated(name, spec)
        if args.rebuild:
            for relative, raw in generated.items():
                (package / relative).write_bytes(raw)
        for relative, raw in generated.items():
            if not (package / relative).is_file() or (package / relative).read_bytes() != raw:
                raise SystemExit(f"DRIFT {name}/{relative}")
        expected = json.loads(generated["expected.json"])
        if expected["skill_release_id"] in seen:
            raise SystemExit("Skill release collision")
        seen.add(expected["skill_release_id"])
        result["skills"][spec[0]] = {"skill_package_hash": expected["skill_package_hash"], "skill_release_id": expected["skill_release_id"]}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
