from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
SDK_ROOT = ROOT / "sdk"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))
PLUGIN_ROOT = ROOT / "plugins" / "source-structure"
MODULE_ROOT = PLUGIN_ROOT / "source_structure"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
EXPECTED_PATH = PLUGIN_ROOT / "expected.json"
REBUILD = ROOT / "tests" / "source" / "structure" / "rebuild_package.py"

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_capability_descriptor,
    verify_manifest,
    verify_package_identity,
)
from source_structure.package_identity import (  # noqa: E402
    calculate_identity,
    canonical_payload_files,
    load_runtime_identity,
)


class StructurePackageTests(unittest.TestCase):
    @staticmethod
    def read_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def installed_env(install_root: Path, *, include_sdk: bool) -> dict[str, str]:
        paths = [str(install_root)]
        if include_sdk:
            paths.append(str(SDK_ROOT))
            for raw in os.environ.get("PYTHONPATH", "").split(os.pathsep):
                if not raw:
                    continue
                candidate = Path(raw)
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if (resolved / "source_structure").is_dir():
                    continue
                normalized = str(resolved)
                if normalized not in paths:
                    paths.append(normalized)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["PYTHONNOUSERSITE"] = "1"
        env["PIP_NO_INDEX"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        return env

    @staticmethod
    def generated_snapshot(expected: dict) -> dict[str, bytes]:
        relatives = {
            "expected.json",
            "files.sha256",
            expected["identity_path"],
            expected["wheel_path"],
            *expected["descriptor_paths"],
            *expected["schema_index_path_list"],
        }
        return {
            relative: (PLUGIN_ROOT / relative).read_bytes()
            for relative in sorted(relatives)
        }

    def test_identity_manifest_and_expected_provider_binding(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        identity = load_runtime_identity()
        digest = calculate_identity(PLUGIN_ROOT)
        self.assertEqual(identity, expected["provider_identity"])
        self.assertEqual(expected["package_hash"], digest.package_hash)
        self.assertEqual(expected["release_id"], digest.release_id)
        self.assertEqual(expected["files_sha256"].encode("utf-8"), (PLUGIN_ROOT / "files.sha256").read_bytes())
        self.assertEqual(expected["identity_path"], "source_structure/identity.json")
        self.assertEqual(expected["provider_identity"]["package_hash"], identity["package_hash"])
        self.assertEqual(expected["provider_identity"]["release_id"], identity["release_id"])
        verify_package_identity(
            canonical_payload_files(),
            expected["plugin_id"],
            expected["version"],
            expected["package_hash"],
            expected["release_id"],
            expected_files_sha256=expected["files_sha256"].encode("utf-8"),
        )

    def test_plugin_manifest_and_all_four_descriptors_match_catalog_projection(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        manifest = self.read_json(PLUGIN_ROOT / "plugin.json")
        verify_manifest(manifest)
        manifest_caps = {item["capability_id"]: item for item in manifest["capabilities"]}
        descriptor_paths = expected["descriptor_paths"]
        self.assertEqual(len(descriptor_paths), 4)
        self.assertEqual(set(manifest_caps), {
            "source.structure.revise/v1",
            "source.evidence.search/v1",
            "source.evidence.rebind.inspect/v1",
            "source.evidence.rebind.propose/v1",
        })
        self.assertEqual(set(expected["descriptor_sha256"]), set(descriptor_paths))
        for relative in descriptor_paths:
            path = PLUGIN_ROOT / relative
            descriptor = self.read_json(path)
            capability = descriptor["capability_id"]
            provider = {"plugin_id": expected["plugin_id"], "release_id": expected["release_id"]}
            verify_capability_descriptor(descriptor, expected_capability_id=capability, allowed_capability_ids=set(manifest_caps), expected_provider=provider)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected["descriptor_sha256"][relative])
            self.assertEqual(descriptor["provider"], expected["descriptor_provider_identity"][relative])
            self.assertEqual(descriptor["accepted_data_formats"], [])
            self.assertEqual(descriptor["supports"], manifest_caps[capability]["operations"])
            self.assertEqual(descriptor["result_contract"], manifest_caps[capability]["result_contract"])

    def test_each_descriptor_has_only_its_two_bound_schema_artifacts(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        actual_indexes = sorted(path.relative_to(PLUGIN_ROOT).as_posix() for path in MODULE_ROOT.rglob("index.json"))
        self.assertEqual(actual_indexes, sorted(expected["schema_index_path_list"]))
        self.assertEqual(set(expected["schema_index_paths"].values()), set(actual_indexes))
        self.assertNotIn("source_structure/schemas/index.json", actual_indexes)
        self.assertEqual(set(expected["schema_index_sha256"]), set(actual_indexes))
        descriptor_by_capability = {
            self.read_json(PLUGIN_ROOT / relative)["capability_id"]: self.read_json(PLUGIN_ROOT / relative)
            for relative in expected["descriptor_paths"]
        }
        for relative in actual_indexes:
            index_path = PLUGIN_ROOT / relative
            index = self.read_json(index_path)
            self.assertEqual(set(index), {"schema", "plugin_id", "capability_id", "schemas"})
            self.assertEqual(index["schema"], "provider-schema-index/v1")
            self.assertEqual(index["plugin_id"], expected["plugin_id"])
            self.assertEqual(expected["schema_index_paths"][index["capability_id"]], relative)
            self.assertIn(index["capability_id"], descriptor_by_capability)
            descriptor = descriptor_by_capability[index["capability_id"]]
            entries = index["schemas"]
            self.assertEqual(len(entries), 2)
            self.assertEqual({entry["schema_id"] for entry in entries}, {descriptor["input_schema"], descriptor["output_schema"]})
            self.assertEqual(entries, expected["schema_artifacts"][relative])
            self.assertEqual(hashlib.sha256(index_path.read_bytes()).hexdigest(), expected["schema_index_sha256"][relative])
            for entry in entries:
                entry_path = Path(entry["path"])
                self.assertFalse(entry_path.is_absolute(), entry["path"])
                self.assertNotIn("..", entry_path.parts, entry["path"])
                self.assertNotIn("\\", entry["path"])
                artifact = MODULE_ROOT / entry["path"]
                self.assertTrue(artifact.is_file(), entry["path"])
                artifact.resolve().relative_to(MODULE_ROOT.resolve())
                self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual(expected["schema_artifact_sha256"][artifact.relative_to(PLUGIN_ROOT).as_posix()], entry["sha256"])

    def test_all_schema_artifacts_are_closed_and_expected_bound(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        actual = {
            path.relative_to(PLUGIN_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in MODULE_ROOT.rglob("*.schema.json")
        }
        self.assertEqual(actual, expected["schema_artifact_sha256"])

        def assert_closed(value, location: str) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False, location)
                for key, child in value.items():
                    assert_closed(child, f"{location}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    assert_closed(child, f"{location}[{index}]")

        for relative in actual:
            schema = self.read_json(PLUGIN_ROOT / relative)
            Draft202012Validator.check_schema(schema)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            if schema.get("type") == "object":
                self.assertIs(schema.get("additionalProperties"), False)
            else:
                self.assertIn("oneOf", schema, relative)
                self.assertIsInstance(schema["oneOf"], list, relative)
            assert_closed(schema, relative)

    def test_wheel_is_deterministic_and_excludes_self_referential_identity(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        wheel = PLUGIN_ROOT / expected["wheel_path"]
        before = self.generated_snapshot(expected)
        first_build = subprocess.run([sys.executable, str(REBUILD)], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
        self.assertIn(expected["plugin_id"], first_build.stdout)
        first = self.generated_snapshot(self.read_json(EXPECTED_PATH))
        second_build = subprocess.run([sys.executable, str(REBUILD)], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
        self.assertIn(expected["plugin_id"], second_build.stdout)
        second = self.generated_snapshot(self.read_json(EXPECTED_PATH))
        self.assertEqual(before, first)
        self.assertEqual(first, second)
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("source_structure/runtime.py", names)
            self.assertIn("source_structure/schemas/revise/index.json", names)
            self.assertNotIn("source_structure/identity.json", names)
            self.assertEqual(sum(name.endswith(".dist-info/METADATA") for name in names), 1)
            self.assertEqual(sum(name.endswith(".dist-info/WHEEL") for name in names), 1)
            self.assertEqual(sum(name.endswith(".dist-info/RECORD") for name in names), 1)
            wheel_metadata = next(archive.read(name).decode("utf-8") for name in names if name.endswith(".dist-info/WHEEL"))
            self.assertIn("Root-Is-Purelib: true", wheel_metadata)
            self.assertIn("Tag: py3-none-any", wheel_metadata)
            record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
            rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
            self.assertEqual(
                [row[0] for row in rows],
                sorted(name for name in names if name != record_name)
                + [record_name],
            )
            for name, encoded_hash, size in rows:
                if name == record_name:
                    self.assertEqual((encoded_hash, size), ("", ""))
                    continue
                data = archive.read(name)
                expected_hash = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
                self.assertEqual(encoded_hash, expected_hash)
                self.assertEqual(size, str(len(data)))

    def test_isolated_wheel_uses_exact_outer_identity_and_descriptors(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        outer_identity = self.read_json(PLUGIN_ROOT / expected["identity_path"])
        outer_descriptors = {
            self.read_json(PLUGIN_ROOT / relative)["capability_id"]: self.read_json(PLUGIN_ROOT / relative)
            for relative in expected["descriptor_paths"]
        }
        wheel = PLUGIN_ROOT / expected["wheel_path"]
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw)
            with zipfile.ZipFile(wheel) as archive:
                self.assertNotIn(expected["identity_path"], archive.namelist())
                archive.extractall(install_root)
            shutil.copyfile(
                PLUGIN_ROOT / expected["identity_path"],
                install_root / expected["identity_path"],
            )
            code = """
import json
from source_structure.package_identity import load_runtime_identity
from source_structure.runtime import CAPABILITIES, PACKAGE_HASH, RELEASE_ID, capability_descriptor, main
print(json.dumps({
    "identity": load_runtime_identity(),
    "package_hash": PACKAGE_HASH,
    "release_id": RELEASE_ID,
    "main": main(),
    "descriptors": {capability: capability_descriptor(capability) for capability in CAPABILITIES},
}, sort_keys=True))
"""
            completed = subprocess.run(
                [sys.executable, "-S", "-c", code],
                cwd=install_root,
                env=self.installed_env(install_root, include_sdk=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        installed = json.loads(completed.stdout)
        self.assertEqual(installed["identity"], outer_identity)
        self.assertEqual(installed["package_hash"], expected["package_hash"])
        self.assertEqual(installed["release_id"], expected["release_id"])
        self.assertEqual(installed["descriptors"], outer_descriptors)
        self.assertEqual(installed["main"], outer_descriptors["source.structure.revise/v1"])

    def test_missing_or_structurally_tampered_installed_sidecar_fails_closed(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        wheel = PLUGIN_ROOT / expected["wheel_path"]
        identity = self.read_json(PLUGIN_ROOT / expected["identity_path"])
        mutations = {
            "schema": {**identity, "schema": "tampered/v1"},
            "owner": {**identity, "plugin_id": "com.example.tampered"},
            "version": {**identity, "version": "9.9.9"},
            "hash": {**identity, "package_hash": "not-a-sha256"},
            "extra": {**identity, "unexpected": True},
        }
        code = "from source_structure.runtime import PACKAGE_HASH; print(PACKAGE_HASH)"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing = root / "missing"
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(missing)
            result = subprocess.run(
                [sys.executable, "-S", "-c", code],
                cwd=missing,
                env=self.installed_env(missing, include_sdk=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("package identity is unavailable", result.stderr)
            for label, value in mutations.items():
                with self.subTest(label=label):
                    install_root = root / label
                    with zipfile.ZipFile(wheel) as archive:
                        archive.extractall(install_root)
                    (install_root / expected["identity_path"]).write_text(
                        json.dumps(value), encoding="utf-8"
                    )
                    result = subprocess.run(
                        [sys.executable, "-S", "-c", code],
                        cwd=install_root,
                        env=self.installed_env(install_root, include_sdk=True),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("package identity", result.stderr)

    def test_descriptor_manifest_schema_wheel_and_identity_tampering_is_detected(self) -> None:
        expected = self.read_json(EXPECTED_PATH)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            descriptor_copy = root / "descriptor"
            shutil.copytree(PLUGIN_ROOT, descriptor_copy)
            descriptor_path = descriptor_copy / expected["descriptor_paths"][0]
            descriptor = self.read_json(descriptor_path)
            descriptor["provider"]["release_id"] = "0" * 64
            descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
            self.assertNotEqual(
                hashlib.sha256(descriptor_path.read_bytes()).hexdigest(),
                expected["descriptor_sha256"][expected["descriptor_paths"][0]],
            )
            with self.assertRaises(Exception):
                verify_capability_descriptor(
                    descriptor,
                    expected_capability_id=descriptor["capability_id"],
                    allowed_capability_ids={descriptor["capability_id"]},
                    expected_provider={
                        "plugin_id": expected["plugin_id"],
                        "release_id": expected["release_id"],
                    },
                )

            for label, relative, mutation in (
                ("manifest", "files.sha256", lambda data: (b"0" if data[:1] != b"0" else b"1") + data[1:]),
                ("schema", "source_structure/schemas/revise/input.schema.json", lambda data: data + b" "),
                ("wheel", expected["wheel_path"], lambda data: data + b"\x00"),
                ("identity", expected["identity_path"], None),
            ):
                with self.subTest(label=label):
                    bundle = root / label
                    shutil.copytree(PLUGIN_ROOT, bundle)
                    path = bundle / relative
                    if label == "identity":
                        value = self.read_json(path)
                        value["release_id"] = "0" * 64
                        path.write_text(json.dumps(value), encoding="utf-8")
                    else:
                        assert mutation is not None
                        path.write_bytes(mutation(path.read_bytes()))
                    with self.assertRaises(Exception):
                        calculate_identity(bundle)

    def test_no_dependency_or_network_fallback(self) -> None:
        lock = (PLUGIN_ROOT / "backend" / "requirements.lock").read_text(encoding="utf-8")
        self.assertFalse([line for line in lock.splitlines() if line.strip() and not line.lstrip().startswith("#")])
        source = "\n".join(
            (MODULE_ROOT / name).read_text(encoding="utf-8")
            for name in ("contract.py", "runtime.py", "package_identity.py")
        )
        for forbidden in (
            "import requests",
            "import httpx",
            "import urllib",
            "import socket",
            "urlopen(",
        ):
            self.assertNotIn(forbidden, source)

        expected = self.read_json(EXPECTED_PATH)
        wheel = PLUGIN_ROOT / expected["wheel_path"]
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(install_root)
            shutil.copyfile(
                PLUGIN_ROOT / expected["identity_path"],
                install_root / expected["identity_path"],
            )
            code = """
import source_structure.runtime as runtime
try:
    runtime._require_sdk()
except runtime.StructureWorkerError as exc:
    print(exc.code)
else:
    raise SystemExit("public SDK unexpectedly resolved")
"""
            completed = subprocess.run(
                [sys.executable, "-S", "-c", code],
                cwd=install_root,
                env=self.installed_env(install_root, include_sdk=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        self.assertEqual(completed.stdout.strip(), "SDK_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
