"""Deterministically rebuild and verify immutable Style Skill packages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"
PAYLOAD = ("method.json", "method.schema.json", "prompt.txt", "skill.json")
STYLE_CODE_PLUGINS = (
    "com.plotpilot.novelagent.style-manufacturing",
    "com.plotpilot.novelagent.style-runtime",
)
SKILLS = {
    "donor-style-analysis": {
        "skill_id": "com.plotpilot.skill.donor.style-analysis",
        "display_name": "供体文风证据分析",
        "stage": "analysis",
        "actions": ["analyze"],
        "formats": ["book-analysis-taxonomy/v1", "lexicon/v1"],
        "capabilities": ["style.manufacture/v1", "style.qualify/v1"],
        "contracts": ["candidate-batch/v1", "diagnostic-bundle/v1"],
    },
    "style-apply": {
        "skill_id": "com.plotpilot.skill.style.apply",
        "display_name": "精确文风应用与返修",
        "stage": "draft",
        "actions": ["draft", "refine"],
        "formats": ["style-pack/v1", "lexicon/v1", "quality-rubric/v1"],
        "capabilities": ["style.apply/v1", "style.review/v1", "style.refine/v1"],
        "contracts": ["candidate-batch/v1", "diagnostic-bundle/v1"],
    },
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[path])}  {path}\n" for path in sorted(files, key=lambda value: value.encode())).encode()


def _release(skill_id: str, package_hash: str) -> str:
    return _sha(f"plotpilot-skill-release/v1\n{skill_id}\n{VERSION}\n{package_hash}\n".encode("ascii"))


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain one JSON object")
    return value


def _generated(name: str, spec: dict[str, Any]) -> dict[str, bytes]:
    package = ROOT / name
    manifest = _object(package / "skill.json")
    if set(manifest) != {"schema", "skill_id", "version", "display_name", "stage", "actions"}:
        raise ValueError(f"{name}/skill.json is not closed")
    if manifest != {"schema": "plotpilot-skill/v1", "skill_id": spec["skill_id"], "version": VERSION,
                    "display_name": spec["display_name"], "stage": spec["stage"], "actions": spec["actions"]}:
        raise ValueError(f"{name} Skill manifest identity drift")
    method = _object(package / "method.json")
    expected_keys = {"schema", "skill_id", "input_format_ids", "capability_ids", "result_contracts", "rules", "authority", "identity_boundary"}
    if set(method) != expected_keys or method["schema"] != "style-skill-method/v1" or method["skill_id"] != spec["skill_id"]:
        raise ValueError(f"{name} method is not closed")
    if method["input_format_ids"] != spec["formats"] or method["capability_ids"] != spec["capabilities"] or method["result_contracts"] != spec["contracts"]:
        raise ValueError(f"{name} frozen method projection drift")
    if method["authority"] != "candidate_or_diagnostic_only" or method["identity_boundary"] != "skill_never_style_or_data":
        raise ValueError(f"{name} authority or identity boundary drift")
    if not (package / "prompt.txt").read_text(encoding="utf-8").strip():
        raise ValueError(f"{name} prompt is empty")
    files = {path: (package / path).read_bytes() for path in PAYLOAD}
    files_sha256 = _manifest(files)
    package_hash = _sha(b"plotpilot-skill-package/v1\n" + files_sha256)
    release_id = _release(spec["skill_id"], package_hash)
    identity = {
        "schema": "style-skill-package-identity/v1", "package_kind": "skill",
        "skill_id": spec["skill_id"], "version": VERSION,
        "skill_package_hash": package_hash, "skill_release_id": release_id,
        "separate_from_style_release_domain": "style-release/v1",
        "separate_from_data_release_domain": "plotpilot-release/v1",
        "separate_from_code_plugin_ids": list(STYLE_CODE_PLUGINS),
    }
    expected = {
        "schema": "style-skill-package-expected/v1", "package_kind": "skill",
        "skill_id": spec["skill_id"], "version": VERSION,
        "manifest_path": "skill.json", "method_path": "method.json",
        "package_files": list(PAYLOAD), "files_sha256": files_sha256.decode(),
        "fixture_hashes": {path: _sha(files[path]) for path in ("method.json", "prompt.txt", "method.schema.json")},
        "skill_package_hash": package_hash, "skill_release_id": release_id,
        "identity_path": "identity.json",
        "identity_domains": {
            "skill": "plotpilot-skill-release/v1", "style": "style-release/v1",
            "data_or_code": "plotpilot-release/v1",
        },
    }
    return {"files.sha256": files_sha256, "identity.json": _json_bytes(identity), "expected.json": _json_bytes(expected)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    summary: dict[str, object] = {"status": "PASS", "skills": {}}
    releases: set[str] = set()
    for name, spec in SKILLS.items():
        generated = _generated(name, spec)
        if args.rebuild:
            for relative, content in generated.items():
                (ROOT / name / relative).write_bytes(content)
        for relative, content in generated.items():
            if not (ROOT / name / relative).is_file() or (ROOT / name / relative).read_bytes() != content:
                raise SystemExit(f"DRIFT {name}/{relative}")
        expected = json.loads(generated["expected.json"])
        if expected["skill_release_id"] in releases:
            raise SystemExit("Skill release collision")
        releases.add(expected["skill_release_id"])
        summary["skills"][spec["skill_id"]] = {
            "skill_package_hash": expected["skill_package_hash"],
            "skill_release_id": expected["skill_release_id"],
        }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
