"""Rebuild and verify the two donor-analysis Skill package identities with stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"
FORMAT_ID = "book-analysis-taxonomy/v1"
CODE_PLUGIN_ID = "com.plotpilot.novelagent.donor-analysis"
PAYLOAD = ("method.json", "method.schema.json", "prompt.txt", "skill.json")
SKILLS = {
    "atomic-breakdown": {
        "skill_id": "com.plotpilot.skill.donor.atomic-breakdown",
        "actions": ["extract", "classify"],
        "capabilities": ["analysis.book.atom.extract/v1", "analysis.book.atom.manual/v1"],
    },
    "claim-synthesis": {
        "skill_id": "com.plotpilot.skill.donor.claim-synthesis",
        "actions": ["synthesize"],
        "capabilities": ["analysis.book.claim.generate/v1"],
    },
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[path])}  {path}\n" for path in sorted(files, key=lambda value: value.encode("utf-8"))).encode("utf-8")


def _release(skill_id: str, package_hash: str) -> str:
    return _sha(f"plotpilot-skill-release/v1\n{skill_id}\n{VERSION}\n{package_hash}\n".encode("ascii"))


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _exact(value: dict[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{name} fields differ: expected={sorted(keys)} actual={sorted(value)}")


def _generated(name: str, spec: dict[str, Any]) -> dict[str, bytes]:
    package = ROOT / name
    manifest = _object(package / "skill.json")
    _exact(manifest, {"schema", "skill_id", "version", "display_name", "stage", "actions"}, f"{name}/skill.json")
    if manifest["schema"] != "plotpilot-skill/v1" or manifest["skill_id"] != spec["skill_id"] or manifest["version"] != VERSION or manifest["stage"] != "analysis" or manifest["actions"] != spec["actions"]:
        raise ValueError(f"{name} Skill manifest identity drift")
    method = _object(package / "method.json")
    _exact(method, {"schema", "skill_id", "input_format_id", "capability_ids", "result_contract", "rules", "authority"}, f"{name}/method.json")
    if method["schema"] != "donor-skill-method/v1" or method["skill_id"] != spec["skill_id"] or method["input_format_id"] != FORMAT_ID or method["capability_ids"] != spec["capabilities"] or method["result_contract"] != "candidate-batch/v1" or method["authority"] != "candidate_only":
        raise ValueError(f"{name} closed method contract drift")
    if not (package / "prompt.txt").read_text(encoding="utf-8").strip():
        raise ValueError(f"{name} prompt is empty")
    files = {path: (package / path).read_bytes() for path in PAYLOAD}
    files_sha256 = _manifest(files)
    package_hash = _sha(b"plotpilot-skill-package/v1\n" + files_sha256)
    release_id = _release(spec["skill_id"], package_hash)
    identity = {
        "schema": "donor-skill-package-identity/v1",
        "package_kind": "skill",
        "skill_id": spec["skill_id"],
        "version": VERSION,
        "skill_package_hash": package_hash,
        "skill_release_id": release_id,
        "separate_from_data_plugin_id": "com.plotpilot.novelagent.book-analysis-taxonomy",
        "separate_from_code_plugin_id": CODE_PLUGIN_ID,
    }
    expected = {
        "schema": "donor-skill-package-expected/v1",
        "package_kind": "skill",
        "skill_id": spec["skill_id"],
        "version": VERSION,
        "manifest_path": "skill.json",
        "method_path": "method.json",
        "package_files": list(PAYLOAD),
        "files_sha256": files_sha256.decode("utf-8"),
        "fixture_hashes": {"method.json": _sha(files["method.json"]), "prompt.txt": _sha(files["prompt.txt"]), "method.schema.json": _sha(files["method.schema.json"])},
        "skill_package_hash": package_hash,
        "skill_release_id": release_id,
        "identity_path": "identity.json",
        "identity_domains": {"skill": "plotpilot-skill-release/v1", "data_or_code": "plotpilot-release/v1", "code_plugin_id": CODE_PLUGIN_ID},
    }
    return {"files.sha256": files_sha256, "identity.json": _json_bytes(identity), "expected.json": _json_bytes(expected)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    summary: dict[str, object] = {"status": "PASS", "skills": {}}
    seen_ids: set[str] = set()
    seen_releases: set[str] = set()
    for name, spec in SKILLS.items():
        if spec["skill_id"] in seen_ids:
            raise SystemExit("duplicate Skill identity")
        generated = _generated(name, spec)
        if args.rebuild:
            for relative, content in generated.items():
                (ROOT / name / relative).write_bytes(content)
        for relative, content in generated.items():
            path = ROOT / name / relative
            if not path.is_file() or path.read_bytes() != content:
                raise SystemExit(f"DRIFT {name}/{relative}")
        expected = json.loads(generated["expected.json"])
        release = expected["skill_release_id"]
        if release in seen_releases:
            raise SystemExit("Skill release collision")
        seen_ids.add(spec["skill_id"])
        seen_releases.add(release)
        summary["skills"][spec["skill_id"]] = {"skill_package_hash": expected["skill_package_hash"], "skill_release_id": release}
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
