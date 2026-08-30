from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "source-cleaning-runtime"))

from source_cleaning_runtime import canonical_bytes  # noqa: E402


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _release(plugin_id: str, version: str, package_hash: str) -> str:
    return _sha(f"plotpilot-release/v1\n{plugin_id}\n{version}\n{package_hash}\n".encode())


class PackageContractTests(unittest.TestCase):
    def test_code_package_manifest_identity_descriptor_and_wheel(self) -> None:
        package = ROOT / "plugins" / "source-cleaning-runtime"
        expected = json.loads((package / "expected.json").read_text(encoding="utf-8"))
        manifest = (package / "files.sha256").read_bytes()
        self.assertEqual(expected["files_sha256"].encode(), manifest)
        self.assertEqual(manifest, manifest.decode("utf-8").encode("utf-8"))
        rows = manifest.decode("utf-8").splitlines()
        names = [row.split("  ", 1)[1] for row in rows]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("files.sha256", names)
        for row in rows:
            digest, name = row.split("  ", 1)
            self.assertEqual(len(digest), 64)
            self.assertEqual(_sha((package / name).read_bytes()), digest)
        package_hash = _sha(b"plotpilot-package/v1\n" + manifest)
        self.assertEqual(package_hash, expected["package_hash"])
        self.assertEqual(
            _release(expected["plugin_id"], expected["version"], package_hash),
            expected["release_id"],
        )
        identity = json.loads((package / "source_cleaning_runtime/identity.json").read_text(encoding="utf-8"))
        self.assertEqual(identity["package_hash"], package_hash)
        self.assertEqual(identity["release_id"], expected["release_id"])
        descriptor = json.loads((package / "descriptor.json").read_text(encoding="utf-8"))
        self.assertEqual(descriptor["provider"]["release_id"], expected["release_id"])
        self.assertEqual(descriptor["capability_id"], "source.clean.preview/v1")
        index_paths = expected["schema_index_paths"]
        self.assertEqual(set(index_paths), {"source.clean.preview/v1", "source.clean.apply/v1", "source.clean.rules.merge/v1"})
        descriptor_paths = {
            "source.clean.preview/v1": "descriptor.json",
            "source.clean.apply/v1": "descriptor-apply.json",
            "source.clean.rules.merge/v1": "descriptor-merge.json",
        }
        for capability_id, index_path in index_paths.items():
            index = json.loads((package / index_path).read_text(encoding="utf-8"))
            descriptor = json.loads((package / descriptor_paths[capability_id]).read_text(encoding="utf-8"))
            self.assertEqual(set(index), {"schema", "plugin_id", "capability_id", "schemas"})
            self.assertEqual(index["schema"], "provider-schema-index/v1")
            self.assertEqual(index["plugin_id"], expected["plugin_id"])
            self.assertEqual(index["capability_id"], capability_id)
            self.assertEqual(
                [(entry["schema_id"], entry["path"]) for entry in index["schemas"]],
                [(descriptor["input_schema"], "schemas/" + Path(expected["input_schema_paths"][{"source.clean.preview/v1": 0, "source.clean.apply/v1": 1, "source.clean.rules.merge/v1": 2}[capability_id]]).name),
                 (descriptor["output_schema"], "schemas/" + Path(expected["output_schema_paths"][{"source.clean.preview/v1": 0, "source.clean.apply/v1": 1, "source.clean.rules.merge/v1": 2}[capability_id]]).name)],
            )
            for entry in index["schemas"]:
                schema_path = package / "source_cleaning_runtime" / entry["path"]
                self.assertEqual(entry["path"].split("/", 1)[0], "schemas")
                self.assertEqual(_sha(schema_path.read_bytes()), entry["sha256"])
        wheel = package / expected["wheel_path"]
        with zipfile.ZipFile(wheel) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("source_cleaning_runtime/runtime.py", archive.namelist())
            self.assertNotIn("source_cleaning_runtime/identity.json", archive.namelist())
            self.assertIn("plotpilot_source_cleaning_runtime-0.1.0.dist-info/RECORD", archive.namelist())
        dependency_wheel = package / "backend/wheels/regex-2026.7.19-cp312-cp312-win_amd64.whl"
        self.assertEqual(_sha(dependency_wheel.read_bytes()), "e30d40268a28d54ce0437031750497004c22602b8e3ab891f759b795a003b312")

    def test_data_package_and_plugin_data_bundle(self) -> None:
        package = ROOT / "data/source-cleaning/rules"
        expected = json.loads((package / "expected.json").read_text(encoding="utf-8"))
        plugin = json.loads((package / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["kind"], "data")
        self.assertNotIn("ui", plugin)
        self.assertNotIn("backend", plugin)
        self.assertNotIn("storage", plugin)
        manifest = (package / "files.sha256").read_bytes()
        self.assertEqual(expected["files_sha256"].encode(), manifest)
        rows = manifest.decode("utf-8").splitlines()
        for row in rows:
            digest, name = row.split("  ", 1)
            self.assertEqual(_sha((package / name).read_bytes()), digest)
        package_hash = _sha(b"plotpilot-package/v1\n" + manifest)
        self.assertEqual(package_hash, expected["package_hash"])
        self.assertEqual(_release("com.plotpilot.novelagent.source-cleaning-rules.webnovel-ads", "1.0.0", package_hash), expected["release_id"])
        bundle = json.loads((package / "fixtures/plugin-data-bundle.json").read_text(encoding="utf-8"))
        unsigned = {key: value for key, value in bundle.items() if key != "bundle_hash"}
        self.assertEqual(bundle["bundle_hash"], _sha(b"plugin-data-bundle/v1\n" + canonical_bytes(unsigned)))
        self.assertEqual(bundle["data_release_id"], expected["release_id"])
        self.assertEqual(bundle["format_id"], "source-cleaning-rules/v1")

    def test_schema_files_are_json_and_closed_at_every_object_definition(self) -> None:
        schema_root = ROOT / "plugins/source-cleaning-runtime/source_cleaning_runtime/schemas"
        preview_request = json.loads((schema_root / "preview-request.schema.json").read_text(encoding="utf-8"))
        apply_request = json.loads((schema_root / "apply-request.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_text", preview_request["properties"])
        self.assertNotIn("source_text", apply_request["properties"])
        self.assertEqual(
            {
                "resume_checkpoint_asset_id",
                "resume_checkpoint_asset_hash",
                "resume_state_asset_id",
                "resume_state_asset_hash",
            },
            {
                key
                for key in (
                    "resume_checkpoint_asset_id",
                    "resume_checkpoint_asset_hash",
                    "resume_state_asset_id",
                    "resume_state_asset_hash",
                )
                if key in apply_request["properties"]
            },
        )
        files = sorted(schema_root.glob("*.json"))
        self.assertEqual(len(files), 9)
        for path in files:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)
            if path.name != "index.json":
                if path.name.endswith("-index.json"):
                    self.assertEqual(set(value), {"schema", "plugin_id", "capability_id", "schemas"})
                    self.assertEqual(value.get("schema"), "provider-schema-index/v1")
                else:
                    self.assertIs(value.get("additionalProperties"), False)
                    self.assertTrue(value.get("$id"))
            else:
                self.fail("legacy aggregate schema index must not exist")

    def test_golden_fixture_has_protected_chinese_context(self) -> None:
        fixture = json.loads((ROOT / "data/source-cleaning/fixtures/cleaning/golden_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema"], "source-cleaning-golden-corpus/v1")
        for case in fixture["cases"]:
            for preserved in case["expected_preserved"]:
                self.assertIn(preserved, case["source_text"])
                self.assertIn(preserved, case["expected_cleaned_text"])
            for removed in case["expected_removed"]:
                self.assertIn(removed, case["source_text"])
                self.assertNotIn(removed, case["expected_cleaned_text"])

    def test_frozen_catalog_capability_result_and_need_alignment(self) -> None:
        package = ROOT / "plugins/source-cleaning-runtime"
        manifest = json.loads((package / "plugin.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "catalog/plugin-catalog-v1.json").read_text(encoding="utf-8"))
        entry = next(item for item in catalog["code_plugins"] if item["plugin_id"] == manifest["plugin_id"])
        self.assertEqual(manifest["needs"], entry["needs"])
        manifest_caps = {item["capability_id"]: item for item in manifest["capabilities"]}
        catalog_caps = {item["capability_id"]: item for item in entry["capabilities"]}
        self.assertEqual(set(manifest_caps), set(catalog_caps))
        descriptor_paths = {
            "source.clean.preview/v1": "descriptor.json",
            "source.clean.apply/v1": "descriptor-apply.json",
            "source.clean.rules.merge/v1": "descriptor-merge.json",
        }
        for capability_id, catalog_cap in catalog_caps.items():
            self.assertEqual(manifest_caps[capability_id]["operations"], catalog_cap["operations"])
            self.assertEqual(manifest_caps[capability_id]["result_contract"], catalog_cap["result_contract"])
            descriptor = json.loads((package / descriptor_paths[capability_id]).read_text(encoding="utf-8"))
            self.assertEqual(descriptor["capability_id"], capability_id)
            self.assertEqual(descriptor["input_schema"], catalog_cap["input_schema"])
            self.assertEqual(descriptor["output_schema"], catalog_cap["output_schema"])
            self.assertEqual(descriptor["result_contract"], catalog_cap["result_contract"])
            self.assertEqual(descriptor["supports"], catalog_cap["operations"])
            self.assertEqual(descriptor["deterministic"], catalog_cap["deterministic"])
            self.assertEqual(descriptor["accepted_data_formats"], catalog_cap["accepted_data_formats"])


if __name__ == "__main__":
    unittest.main()
