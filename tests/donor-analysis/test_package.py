from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk"
PLUGIN_ROOT = ROOT / "plugins" / "donor-analysis"
MODULE_ROOT = PLUGIN_ROOT / "donor_analysis"
for path in (SDK_ROOT, PLUGIN_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from donor_analysis.package_identity import (
    calculate_identity,
    canonical_payload_files,
    load_runtime_identity,
)
from plotpilot_plugin_sdk.package import digest_package
from plotpilot_plugin_sdk.verifier import (
    verify_capability_descriptor,
    verify_manifest,
    verify_package_identity,
)

FROZEN_CAPABILITY_SEMANTICS = {
    "analysis.book.atom.extract/v1": {
        "input_schema": "analysis.book.atom.extract-request/v1",
        "output_schema": "analysis.book.atom.extract-result/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
    },
    "analysis.book.atom.manual/v1": {
        "input_schema": "analysis.book.atom.manual-request/v1",
        "output_schema": "analysis.book.atom.manual-result/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "validate"],
        "deterministic": True,
    },
    "analysis.book.claim.generate/v1": {
        "input_schema": "analysis.book.claim.generate-request/v1",
        "output_schema": "analysis.book.claim.generate-result/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
    },
    "analysis.book.rereview/v1": {
        "input_schema": "analysis.book.rereview-request/v1",
        "output_schema": "analysis.book.rereview-result/v1",
        "result_contract": "diagnostic-bundle/v1",
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
    },
}
EXACT_NEEDS = [
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.model.invoke/v1",
    "host.candidate.stage/v1",
]
EXACT_SLOTS = {
    "donors.analysis.configure",
    "job.drawer.detail",
    "donors.analysis.review",
    "donors.evidence.inspector",
}


def strict_json(path: Path):
    def reject_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def assert_closed(value, location="$") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert value.get("additionalProperties") is False, location
        for key, child in value.items():
            assert_closed(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_closed(child, f"{location}[{index}]")


def runtime_descriptors() -> dict[str, dict]:
    from donor_analysis import runtime

    return {
        capability_id: runtime.capability_descriptor(capability_id)
        for capability_id in runtime.CAPABILITIES
    }


def capability_source_path(plugin_root: Path = PLUGIN_ROOT) -> Path:
    """Locate the sole Python source that owns the complete capability projection."""
    semantic_needles = {
        *FROZEN_CAPABILITY_SEMANTICS,
        *(value["input_schema"] for value in FROZEN_CAPABILITY_SEMANTICS.values()),
        *(value["output_schema"] for value in FROZEN_CAPABILITY_SEMANTICS.values()),
        "result_contract",
        "supports",
        "deterministic",
    }
    candidates = []
    for path in sorted((plugin_root / "donor_analysis").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if all(needle in text for needle in semantic_needles):
            candidates.append(path)
    assert len(candidates) == 1, (
        "the complete capability semantics must occur in exactly one Python source, "
        f"not disconnected copies: {[path.name for path in candidates]}"
    )
    return candidates[0]


def copy_plugin_repository(destination: Path) -> tuple[Path, Path]:
    repository = destination / "repository"
    copied_plugin = repository / "plugins" / "donor-analysis"
    shutil.copytree(PLUGIN_ROOT, copied_plugin)
    shutil.copytree(SDK_ROOT, repository / "sdk")
    shutil.copytree(ROOT / "contracts", repository / "contracts")
    return repository, copied_plugin


def package_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def run_rebuild(repository: Path, plugin_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(plugin_root / "rebuild_package.py")],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def rebuilt_plugin_copy(destination: Path) -> tuple[Path, Path]:
    repository, copied_plugin = copy_plugin_repository(destination)
    completed = run_rebuild(repository, copied_plugin)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return repository, copied_plugin


def wheel_record_rows(wheel: Path) -> tuple[zipfile.ZipFile, str, list[list[str]]]:
    archive = zipfile.ZipFile(wheel)
    record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
    assert len(record_names) == 1
    record_name = record_names[0]
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    return archive, record_name, rows


def assert_wheel_record(wheel: Path) -> None:
    archive, record_name, rows = wheel_record_rows(wheel)
    with archive:
        names = archive.namelist()
        assert archive.testzip() is None
        assert len(names) == len(set(names))
        assert [row[0] for row in rows] == sorted(
            (name for name in names if name != record_name), key=lambda value: value.encode("utf-8")
        ) + [record_name]
        for name, encoded_hash, size in rows:
            if name == record_name:
                assert (encoded_hash, size) == ("", "")
                continue
            data = archive.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
            assert encoded_hash == "sha256=" + encoded
            assert size == str(len(data))


def rewrite_wheel_runtime(wheel: Path, *, synchronize_record: bool) -> None:
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    runtime_name = "donor_analysis/runtime.py"
    record_name = next(name for name in contents if name.endswith(".dist-info/RECORD"))
    contents[runtime_name] += b"\n# wheel-content-drift\n"
    if synchronize_record:
        rows = list(csv.reader(io.StringIO(contents[record_name].decode("utf-8"))))
        for row in rows:
            if row[0] == runtime_name:
                data = contents[runtime_name]
                encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
                row[1:] = ["sha256=" + encoded, str(len(data))]
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        contents[record_name] = output.getvalue().encode("utf-8")
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(contents, key=lambda value: value.encode("utf-8")):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, contents[name])


def mutate_manual_capability_source(path: Path, field: str) -> None:
    text = path.read_text(encoding="utf-8")
    capability = "analysis.book.atom.manual/v1"
    boundaries = list(FROZEN_CAPABILITY_SEMANTICS)
    blocks: list[tuple[int, int]] = []
    for match in re.finditer(re.escape(capability), text):
        following = [
            text.find(other, match.end())
            for other in boundaries
            if other != capability and text.find(other, match.end()) >= 0
        ]
        end = min(following) if following else min(len(text), match.end() + 3000)
        block = text[match.start():end]
        if all(
            token in block
            for token in (
                "analysis.book.atom.manual-request/v1",
                "analysis.book.atom.manual-result/v1",
                "candidate-batch/v1",
                "validate",
            )
        ):
            blocks.append((match.start(), end))
    assert len(blocks) == 1, "manual capability must have one mutable source block"
    start, end = blocks[0]
    block = text[start:end]
    if field == "supports":
        changed = block.replace('"validate"', '"cancel"', 1)
    elif field == "deterministic":
        changed, count = re.subn(r"\bTrue\b", "False", block, count=1)
        assert count == 1
    elif field == "schema":
        changed = block.replace(
            "analysis.book.atom.manual-result/v1",
            "analysis.book.atom.manual-result.changed/v1",
            1,
        )
    elif field == "result":
        changed = block.replace("candidate-batch/v1", "diagnostic-bundle/v1", 1)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(field)
    assert changed != block
    path.write_text(text[:start] + changed + text[end:], encoding="utf-8", newline="\n")


def installed_runtime_descriptors(repository: Path, plugin_root: Path) -> dict[str, dict]:
    expected = strict_json(plugin_root / "expected.json")
    wheel = plugin_root / expected["wheel_path"]
    install_root = repository / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(install_root)
    identity_target = install_root / expected["identity_path"]
    identity_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(plugin_root / expected["identity_path"], identity_target)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(install_root), str(repository / "sdk")])
    env["PYTHONNOUSERSITE"] = "1"
    code = (
        "import json; from donor_analysis.runtime import CAPABILITIES, capability_descriptor; "
        "print(json.dumps({c:capability_descriptor(c) for c in CAPABILITIES},sort_keys=True))"
    )
    completed = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def test_manifest_has_exact_frozen_capabilities_needs_and_metadata_only_slots() -> None:
    manifest = strict_json(PLUGIN_ROOT / "plugin.json")
    descriptors = runtime_descriptors()
    verify_manifest(manifest)
    assert manifest["plugin_id"] == "com.plotpilot.novelagent.donor-analysis"
    assert manifest["kind"] == "code"
    assert manifest["needs"] == EXACT_NEEDS
    assert set(descriptors) == set(FROZEN_CAPABILITY_SEMANTICS)
    assert {item["capability_id"] for item in manifest["capabilities"]} == set(descriptors)
    for capability in manifest["capabilities"]:
        runtime = descriptors[capability["capability_id"]]
        assert capability["operations"] == runtime["supports"]
        assert capability["result_contract"] == runtime["result_contract"]
    contributions = manifest["ui"]["contributions"]
    assert {item["slot"] for item in contributions} == EXACT_SLOTS
    assert all(item["capability_id"] in descriptors for item in contributions)
    ui_metadata = strict_json(PLUGIN_ROOT / manifest["ui"]["entry"])
    assert ui_metadata["implementation"] == "host-projected-metadata-only"
    assert set(ui_metadata["slots"]) == EXACT_SLOTS
    assert not list((PLUGIN_ROOT / "ui").glob("*.js"))
    assert not list((PLUGIN_ROOT / "ui").glob("*.html"))


def test_all_four_descriptors_match_manifest_and_exact_catalog_projection() -> None:
    expected = strict_json(PLUGIN_ROOT / "expected.json")
    runtime = runtime_descriptors()
    provider = {
        "plugin_id": expected["plugin_id"],
        "release_id": expected["release_id"],
    }
    assert len(expected["descriptor_paths"]) == 4
    observed = set()
    for relative in expected["descriptor_paths"]:
        descriptor = strict_json(PLUGIN_ROOT / relative)
        capability = descriptor["capability_id"]
        observed.add(capability)
        verify_capability_descriptor(
            descriptor,
            expected_capability_id=capability,
            allowed_capability_ids=set(runtime),
            expected_provider=provider,
        )
        assert descriptor == runtime[capability]
        assert hashlib.sha256((PLUGIN_ROOT / relative).read_bytes()).hexdigest() == expected["descriptor_sha256"][relative]
    assert observed == set(runtime)


def test_one_capability_spec_source_drives_runtime_and_preserves_exact_frozen_semantics() -> None:
    source = capability_source_path()
    assert source.parent == MODULE_ROOT
    descriptors = runtime_descriptors()
    projection = {
        capability_id: {
            field: descriptor[field]
            for field in ("input_schema", "output_schema", "result_contract", "supports", "deterministic")
        }
        for capability_id, descriptor in descriptors.items()
    }
    assert projection == FROZEN_CAPABILITY_SEMANTICS


def test_package_identity_files_manifest_and_separate_release_domain() -> None:
    expected = strict_json(PLUGIN_ROOT / "expected.json")
    identity = strict_json(PLUGIN_ROOT / expected["identity_path"])
    digest = calculate_identity(PLUGIN_ROOT)
    assert digest.package_hash == expected["package_hash"]
    assert digest.release_id == expected["release_id"]
    assert identity == expected["provider_identity"] == load_runtime_identity()
    verify_package_identity(
        canonical_payload_files(),
        expected["plugin_id"],
        expected["version"],
        expected["package_hash"],
        expected["release_id"],
        expected_files_sha256=(PLUGIN_ROOT / "files.sha256").read_bytes(),
    )
    data = strict_json(ROOT / "data" / "book-analysis-taxonomy" / "v1" / "expected.json")
    atomic = strict_json(ROOT / "skills" / "donor-analysis" / "atomic-breakdown" / "expected.json")
    claim = strict_json(ROOT / "skills" / "donor-analysis" / "claim-synthesis" / "expected.json")
    assert len(
        {
            expected["package_hash"],
            data["package_hash"],
            atomic["skill_package_hash"],
            claim["skill_package_hash"],
        }
    ) == 4
    assert len(
        {
            expected["release_id"],
            data["release_id"],
            atomic["skill_release_id"],
            claim["skill_release_id"],
        }
    ) == 4


def test_schema_indexes_and_every_object_schema_are_closed_and_hash_bound() -> None:
    expected = strict_json(PLUGIN_ROOT / "expected.json")
    indexes = sorted(path.relative_to(PLUGIN_ROOT).as_posix() for path in MODULE_ROOT.rglob("index.json"))
    assert indexes == sorted(expected["schema_index_path_list"])
    for relative in indexes:
        index = strict_json(PLUGIN_ROOT / relative)
        assert set(index) == {"schema", "plugin_id", "capability_id", "schemas"}
        assert index["schema"] == "provider-schema-index/v1"
        assert index["plugin_id"] == expected["plugin_id"]
        assert index["schemas"] == expected["schema_artifacts"][relative]
        assert hashlib.sha256((PLUGIN_ROOT / relative).read_bytes()).hexdigest() == expected["schema_index_sha256"][relative]
        for item in index["schemas"]:
            path = MODULE_ROOT / item["path"]
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    actual = {
        path.relative_to(PLUGIN_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in MODULE_ROOT.rglob("*.schema.json")
    }
    assert actual == expected["schema_artifact_sha256"]
    for relative in actual:
        schema = strict_json(PLUGIN_ROOT / relative)
        Draft202012Validator.check_schema(schema)
        assert_closed(schema, relative)


def test_strict_json_all_package_json_files_and_no_direct_authority_or_network_fallback() -> None:
    for path in PLUGIN_ROOT.rglob("*.json"):
        strict_json(path)
    lock_lines = [
        line
        for line in (PLUGIN_ROOT / "backend" / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lock_lines == []
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in MODULE_ROOT.glob("*.py")
    ).lower()
    for forbidden in (
        "import sqlite3",
        "import requests",
        "import httpx",
        "import socket",
        "urlopen(",
        "fastapi",
        "flask",
        "router(",
        "publication_operation_key",
        "acceptance_ordinal =",
    ):
        assert forbidden not in source


def test_wheel_record_is_valid_and_unchanged_double_rebuild_is_byte_stable(tmp_path: Path) -> None:
    repository, copied_plugin = copy_plugin_repository(tmp_path)
    first = run_rebuild(repository, copied_plugin)
    assert first.returncode == 0, first.stdout + first.stderr
    assert strict_json(copied_plugin / "expected.json")["plugin_id"] in first.stdout
    after_first = package_snapshot(copied_plugin)
    second = run_rebuild(repository, copied_plugin)
    assert second.returncode == 0, second.stdout + second.stderr
    assert package_snapshot(copied_plugin) == after_first
    expected = strict_json(copied_plugin / "expected.json")
    wheel = copied_plugin / expected["wheel_path"]
    assert_wheel_record(wheel)
    with zipfile.ZipFile(wheel) as archive:
        assert "donor_analysis/runtime.py" in archive.namelist()
        assert "donor_analysis/identity.json" not in archive.namelist()


def test_isolated_wheel_import_uses_exact_outer_identity_and_tamper_fails_closed() -> None:
    expected = strict_json(PLUGIN_ROOT / "expected.json")
    wheel = PLUGIN_ROOT / expected["wheel_path"]
    with tempfile.TemporaryDirectory() as raw:
        install_root = Path(raw)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(install_root)
        identity_target = install_root / expected["identity_path"]
        identity_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN_ROOT / expected["identity_path"], identity_target)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(install_root), str(SDK_ROOT)])
        env["PYTHONNOUSERSITE"] = "1"
        code = (
            "import json; from donor_analysis.runtime import PACKAGE_HASH, RELEASE_ID, DESCRIPTORS; "
            "print(json.dumps({'package_hash':PACKAGE_HASH,'release_id':RELEASE_ID,'descriptors':DESCRIPTORS},sort_keys=True))"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=install_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        installed = json.loads(completed.stdout)
        assert installed["package_hash"] == expected["package_hash"]
        assert installed["release_id"] == expected["release_id"]
        assert set(installed["descriptors"]) == set(FROZEN_CAPABILITY_SEMANTICS)
        identity = strict_json(identity_target)
        identity["release_id"] = "0" * 64
        identity_target.write_text(json.dumps(identity), encoding="utf-8")
        tampered = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=install_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert tampered.returncode != 0
        assert "identity" in tampered.stderr.lower()


def test_outer_manifest_descriptor_schema_wheel_and_identity_tamper_are_detected(tmp_path: Path) -> None:
    _, canonical = rebuilt_plugin_copy(tmp_path)
    expected = strict_json(canonical / "expected.json")
    for label, relative in (
        ("manifest", "plugin.json"),
        ("descriptor", expected["descriptor_paths"][0]),
        ("schema", "donor_analysis/schemas/evidence-span.schema.json"),
        ("wheel", expected["wheel_path"]),
        ("identity", expected["identity_path"]),
    ):
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / label
            shutil.copytree(canonical, bundle)
            path = bundle / relative
            if label == "identity":
                identity = strict_json(path)
                identity["release_id"] = "0" * 64
                path.write_text(json.dumps(identity), encoding="utf-8")
            else:
                path.write_bytes(path.read_bytes() + b" ")
            with pytest.raises((RuntimeError, ValueError, OSError)):
                calculate_identity(bundle)


def test_f007_descriptor_and_expected_synchronized_tamper_still_fails_closed(tmp_path: Path) -> None:
    _, bundle = rebuilt_plugin_copy(tmp_path)
    with tempfile.TemporaryDirectory() as raw:
        tampered = Path(raw) / "descriptor-sync-tamper"
        shutil.copytree(bundle, tampered)
        descriptor_path = tampered / "descriptor.json"
        descriptor = strict_json(descriptor_path)
        descriptor["supports"] = ["run"]
        descriptor["deterministic"] = True
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
        expected_path = tampered / "expected.json"
        expected = strict_json(expected_path)
        expected["descriptor_sha256"]["descriptor.json"] = hashlib.sha256(
            descriptor_path.read_bytes()
        ).hexdigest()
        expected_path.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        with pytest.raises((RuntimeError, ValueError, OSError)):
            calculate_identity(tampered)


def test_f007_undeclared_outer_package_file_is_rejected(tmp_path: Path) -> None:
    _, bundle = rebuilt_plugin_copy(tmp_path)
    (bundle / "undeclared-package-file.txt").write_text("not declared\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="inventory"):
        calculate_identity(bundle)


def test_runtime_descriptors_generated_descriptors_and_plugin_manifest_are_one_projection() -> None:
    capability_source_path()
    runtime = runtime_descriptors()
    manifest = strict_json(PLUGIN_ROOT / "plugin.json")
    manifest_by_id = {item["capability_id"]: item for item in manifest["capabilities"]}
    expected = strict_json(PLUGIN_ROOT / "expected.json")
    outer_by_id = {
        descriptor["capability_id"]: descriptor
        for descriptor in (strict_json(PLUGIN_ROOT / path) for path in expected["descriptor_paths"])
    }
    assert set(runtime) == set(manifest_by_id) == set(outer_by_id)
    for capability_id, descriptor in runtime.items():
        assert outer_by_id[capability_id] == descriptor
        assert manifest_by_id[capability_id] == {
            "capability_id": capability_id,
            "operations": descriptor["supports"],
            "result_contract": descriptor["result_contract"],
        }


@pytest.mark.parametrize("field", ["supports", "deterministic", "schema", "result"])
def test_actual_runtime_capability_source_semantic_change_rejects_or_reidentifies_package(
    tmp_path: Path, field: str
) -> None:
    repository, copied_plugin = rebuilt_plugin_copy(tmp_path)
    baseline = strict_json(copied_plugin / "expected.json")
    source = capability_source_path(copied_plugin)
    mutate_manual_capability_source(source, field)
    rebuilt = run_rebuild(repository, copied_plugin)
    if rebuilt.returncode != 0:
        rejection = (rebuilt.stdout + rebuilt.stderr).lower()
        assert any(
            signal in rejection
            for signal in (
                "capability",
                "descriptor",
                "deterministic",
                "manifest",
                "result_contract",
                "schema",
                "supports",
            )
        ), rejection
        return

    changed = strict_json(copied_plugin / "expected.json")
    assert (changed["package_hash"], changed["release_id"]) != (
        baseline["package_hash"],
        baseline["release_id"],
    )
    installed = installed_runtime_descriptors(repository, copied_plugin)
    outer = {
        descriptor["capability_id"]: descriptor
        for descriptor in (strict_json(copied_plugin / path) for path in changed["descriptor_paths"])
    }
    manifest = strict_json(copied_plugin / "plugin.json")
    manifest_by_id = {item["capability_id"]: item for item in manifest["capabilities"]}
    assert installed == outer
    for capability_id, descriptor in installed.items():
        assert manifest_by_id[capability_id]["operations"] == descriptor["supports"]
        assert manifest_by_id[capability_id]["result_contract"] == descriptor["result_contract"]


@pytest.mark.parametrize("synchronize_record", [False, True], ids=["stale-record", "synchronized-record"])
def test_wheel_content_or_record_drift_is_rejected_and_changes_candidate_identity(
    tmp_path: Path, synchronize_record: bool
) -> None:
    _, copied_plugin = rebuilt_plugin_copy(tmp_path)
    expected = strict_json(copied_plugin / "expected.json")
    wheel = copied_plugin / expected["wheel_path"]
    rewrite_wheel_runtime(wheel, synchronize_record=synchronize_record)
    if synchronize_record:
        assert_wheel_record(wheel)
    else:
        with pytest.raises(AssertionError):
            assert_wheel_record(wheel)
    with pytest.raises((RuntimeError, ValueError, OSError)):
        calculate_identity(copied_plugin)
    candidate = digest_package(
        canonical_payload_files(copied_plugin),
        expected["plugin_id"],
        expected["version"],
    )
    assert (candidate.package_hash, candidate.release_id) != (
        expected["package_hash"],
        expected["release_id"],
    )


@pytest.mark.parametrize(
    "relative",
    ["files.sha256", "expected.json", "donor_analysis/identity.json"],
    ids=["files-manifest", "expected-manifest", "outer-identity"],
)
def test_missing_package_manifests_or_outer_identity_fail_closed(tmp_path: Path, relative: str) -> None:
    _, copied_plugin = rebuilt_plugin_copy(tmp_path)
    (copied_plugin / relative).unlink()
    with pytest.raises((RuntimeError, ValueError, OSError)):
        calculate_identity(copied_plugin)


@pytest.mark.parametrize("field", ["plugin_id", "version", "package_hash", "release_id"])
def test_outer_identity_owner_version_or_digest_mismatch_fails_closed(tmp_path: Path, field: str) -> None:
    _, copied_plugin = rebuilt_plugin_copy(tmp_path)
    expected = strict_json(copied_plugin / "expected.json")
    identity_path = copied_plugin / expected["identity_path"]
    identity = strict_json(identity_path)
    identity[field] = "mismatch" if field in {"plugin_id", "version"} else "f" * 64
    identity_path.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises((RuntimeError, ValueError, OSError)):
        calculate_identity(copied_plugin)
