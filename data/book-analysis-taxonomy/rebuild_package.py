"""Rebuild and verify the book-analysis-taxonomy/v1 Data package with stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent / "v1"
PLUGIN_ID = "com.plotpilot.novelagent.book-analysis-taxonomy"
CODE_PLUGIN_ID = "com.plotpilot.novelagent.donor-analysis"
FORMAT_ID = "book-analysis-taxonomy/v1"
VERSION = "1.0.0"
PAYLOAD = (
    "data/taxonomy.json",
    "fixtures/interpreter-roundtrip.json",
    "plugin.json",
    "schemas/book-analysis-taxonomy-v1.schema.json",
    "schemas/interpreter-roundtrip-v1.schema.json",
)
CAPABILITIES = (
    "analysis.book.atom.extract/v1",
    "analysis.book.atom.manual/v1",
    "analysis.book.claim.generate/v1",
    "analysis.book.rereview/v1",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[path])}  {path}\n" for path in sorted(files, key=lambda value: value.encode("utf-8"))).encode("utf-8")


def _release(package_hash: str) -> str:
    return _sha(f"plotpilot-release/v1\n{PLUGIN_ID}\n{VERSION}\n{package_hash}\n".encode("ascii"))


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads((ROOT / path).read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _exact(value: dict[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{name} fields differ: expected={sorted(keys)} actual={sorted(value)}")


def _validate_taxonomy() -> None:
    value = _load_json("data/taxonomy.json")
    _exact(value, {"schema", "format_id", "taxonomy_id", "version", "atom_kinds", "description_types", "narration_techniques", "ratio_metrics", "rhythm_emotion_metrics", "language_metrics", "structure_metrics", "review_rubric", "interpreter_mappings"}, "taxonomy")
    if value["schema"] != FORMAT_ID or value["format_id"] != FORMAT_ID or value["version"] != VERSION:
        raise ValueError("taxonomy identity drift")
    expected_sets = {
        "description_types": {"scenery", "appearance", "action", "combat", "psychology", "environment", "object"},
        "narration_techniques": {"line_drawing", "fine_detail", "stream_of_consciousness", "symbolism", "metaphor", "contrast"},
        "ratio_metrics": {"action_ratio", "psychology_ratio", "scenery_ratio"},
        "rhythm_emotion_metrics": {"chapter_rhythm", "emotion_curve", "climax_buffer_sequence"},
        "language_metrics": {"lexicon_and_idiom", "viewpoint", "grammatical_person", "tense"},
        "structure_metrics": {"chapter_ending", "sentence_length", "paragraph_length", "segmentation", "punctuation_habits"},
    }
    for field, expected in expected_sets.items():
        items = value[field]
        if not isinstance(items, list) or {item.get("key") for item in items if isinstance(item, dict)} != expected:
            raise ValueError(f"frozen {field} coverage drift")
        item_keys = {"key", "label_zh"} if field in {"description_types", "narration_techniques"} else {"key", "label_zh", "value_type", "allowed_values"}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{field}[{index}] must be an object")
            _exact(item, item_keys, f"{field}[{index}]")
    for field in ("atom_kinds",):
        for index, item in enumerate(value[field]):
            _exact(item, {"key", "label_zh"}, f"{field}[{index}]")
    for index, item in enumerate(value["review_rubric"]):
        _exact(item, {"criterion_id", "label_zh", "applies_to", "requirement", "failure_code"}, f"review_rubric[{index}]")
    expected_pairs = {(capability, direction) for capability in CAPABILITIES for direction in ("format_to_capability", "capability_to_format")}
    actual_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(value["interpreter_mappings"]):
        _exact(item, {"format_id", "plugin_id", "capability_id", "direction"}, f"interpreter_mappings[{index}]")
        if item["format_id"] != FORMAT_ID or item["plugin_id"] != CODE_PLUGIN_ID:
            raise ValueError("interpreter identity drift")
        actual_pairs.add((item["capability_id"], item["direction"]))
    if actual_pairs != expected_pairs or len(value["interpreter_mappings"]) != len(expected_pairs):
        raise ValueError("interpreter mapping is not an exact bidirectional mapping for all four capabilities")
    fixture = _load_json("fixtures/interpreter-roundtrip.json")
    _exact(fixture, {"schema", "format_id", "plugin_id", "capabilities", "forward_direction", "reverse_direction"}, "interpreter fixture")
    if tuple(fixture["capabilities"]) != CAPABILITIES:
        raise ValueError("interpreter round-trip fixture order drift")


def _generated() -> dict[str, bytes]:
    _validate_taxonomy()
    files = {path: (ROOT / path).read_bytes() for path in PAYLOAD}
    manifest = _manifest(files)
    package_hash = _sha(b"plotpilot-package/v1\n" + manifest)
    release_id = _release(package_hash)
    root = ROOT / "data/taxonomy.json"
    bundle_without_hash = {
        "schema": "plugin-data-bundle/v1",
        "bundle_id": "book-analysis-taxonomy-v1-bundle",
        "data_plugin_id": PLUGIN_ID,
        "data_release_id": release_id,
        "package_hash": package_hash,
        "format_id": FORMAT_ID,
        "root_path": "data/taxonomy.json",
        "files": [{
            "path": "data/taxonomy.json",
            "asset_id": "asset-book-analysis-taxonomy-v1",
            "sha256": _sha(root.read_bytes()),
            "mime": "application/json",
            "size": len(root.read_bytes()),
        }],
    }
    bundle = {**bundle_without_hash, "bundle_hash": _sha(b"plugin-data-bundle/v1\n" + _canonical(bundle_without_hash))}
    identity = {
        "schema": "book-analysis-taxonomy-package-identity/v1",
        "package_kind": "data",
        "plugin_id": PLUGIN_ID,
        "format_id": FORMAT_ID,
        "version": VERSION,
        "package_hash": package_hash,
        "release_id": release_id,
        "separate_from_code_plugin_id": CODE_PLUGIN_ID,
    }
    expected = {
        "schema": "book-analysis-taxonomy-expected/v1",
        "package_kind": "data",
        "plugin_id": PLUGIN_ID,
        "format_id": FORMAT_ID,
        "version": VERSION,
        "manifest_path": "plugin.json",
        "root_path": "data/taxonomy.json",
        "package_files": list(PAYLOAD),
        "files_sha256": manifest.decode("utf-8"),
        "fixture_hashes": {path: _sha((ROOT / path).read_bytes()) for path in ("fixtures/interpreter-roundtrip.json", "schemas/book-analysis-taxonomy-v1.schema.json", "schemas/interpreter-roundtrip-v1.schema.json", "data/taxonomy.json")},
        "package_hash": package_hash,
        "release_id": release_id,
        "bundle_hash": bundle["bundle_hash"],
        "identity_path": "identity.json",
        "identity_domains": {"data_or_code": "plotpilot-release/v1", "skill": "plotpilot-skill-release/v1", "code_plugin_id": CODE_PLUGIN_ID},
    }
    return {
        "files.sha256": manifest,
        "identity.json": _json_bytes(identity),
        "expected.json": _json_bytes(expected),
        "fixtures/plugin-data-bundle.json": _json_bytes(bundle),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    generated = _generated()
    if args.rebuild:
        for name, content in generated.items():
            path = ROOT / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    for name, expected in generated.items():
        path = ROOT / name
        if not path.is_file() or path.read_bytes() != expected:
            raise SystemExit(f"DRIFT {name}")
    print(json.dumps({"format_id": FORMAT_ID, "package_hash": json.loads(generated["expected.json"])["package_hash"], "release_id": json.loads(generated["expected.json"])["release_id"], "status": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
