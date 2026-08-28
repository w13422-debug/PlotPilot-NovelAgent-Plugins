"""Strict NAP-00 B0 source-candidate delivery gate.

The CLI validates one exact, clean *source* commit.  It deliberately refuses
to infer the candidate from the ambient branch tip or an evidence commit.  The
content checks bind the frozen catalog, all three demo package identities and
the two provider package/descriptor identities.  It never merges, starts the
application, or makes a review decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    verify_catalog,
    verify_capability_descriptor,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_package_manifest,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_skill_identity,
    verify_skill_receipt,
)
BASELINE = "c1b9519c7d25ce1fbef07984cdb548c89e7e1152"
BRANCH = "codex/nap-00-b0-g2"
GENERATION_PARENT = "3cfd6619959c16d394655bda170557b96052e6c9"
REJECTED_SOURCE = "db434847edb84ac337bb4e11d2a6f3a09912eb3b"
REJECTED_EVIDENCE = "480d0f464b474ca2c62714803cd28f5c3595c045"
FINDING_MANIFEST_PATH = "coordination/NAP-00/sol-b0-independent-review-v1.json"
FINDING_MANIFEST_COMMIT = "3cfd6619959c16d394655bda170557b96052e6c9"
FINDING_MANIFEST_SHA256 = "13d418ebc6096210b7c866da7fc4c1da2919f06185b2ef9666b9d69557da77cb"
FINDING_MANIFEST_SIZE = 22433
FINDING_MANIFEST_PRECOMMIT_WORKTREE_SHA256 = "432dbe4d36ed37fd09f737a621370fbb9ed56ed1389e031ca130e6b046765159"
ALLOWED_PREFIXES = (
    "contracts/",
    "sdk/",
    "catalog/",
    "plugins/provider-anthropic/",
    "plugins/provider-gemini/",
    "tools/integration/",
    "tests/contracts/",
    "tests/providers/",
    "tests/e2e/",
    "docs/deliveries/",
    "coordination/NAP-00/",
    "coordination/integration/",
)

PROVIDERS = (
    (
        "provider-anthropic",
        "com.plotpilot.novelagent.provider-anthropic",
        "model.provider.anthropic.invoke/v1",
    ),
    (
        "provider-gemini",
        "com.plotpilot.novelagent.provider-gemini",
        "model.provider.gemini.invoke/v1",
    ),
)
PROVIDER_NEEDS = [
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.job.event/v1",
    "host.job.complete/v1",
    "host.checkpoint.commit/v1",
    "host.stream.commit/v1",
]


@dataclass(frozen=True)
class SourceIdentity:
    baseline_head: str
    expected_head: str
    expected_tree: str
    branch: str


def _git(*args: str, root: Path = ROOT) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _git_bytes(*args: str, root: Path = ROOT) -> bytes:
    completed = subprocess.run(["git", *args], cwd=root, check=False, capture_output=True)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return completed.stdout


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def committed_paths(root: Path, baseline_head: str, expected_head: str) -> list[str]:
    return sorted(path for path in _git("diff", "--name-only", f"{baseline_head}..{expected_head}", root=root).splitlines() if path)


def check_frozen_manifest_blob(root: Path, expected_head: str) -> list[str]:
    """Bind the frozen Finding Manifest to committed Git bytes, never checkout EOLs."""

    failures: list[str] = []
    try:
        frozen_spec = f"{FINDING_MANIFEST_COMMIT}:{FINDING_MANIFEST_PATH}"
        source_spec = f"{expected_head}:{FINDING_MANIFEST_PATH}"
        frozen_oid = _git("rev-parse", frozen_spec, root=root)
        source_oid = _git("rev-parse", source_spec, root=root)
        committed = _git_bytes("cat-file", "blob", frozen_oid, root=root)
    except RuntimeError as exc:
        return [f"frozen Finding Manifest commit blob cannot be resolved: {exc}"]
    if source_oid != frozen_oid:
        failures.append("frozen Finding Manifest blob changed in the source candidate")
    if len(committed) != FINDING_MANIFEST_SIZE or hashlib.sha256(committed).hexdigest() != FINDING_MANIFEST_SHA256:
        failures.append("frozen Finding Manifest committed blob identity mismatch")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", FINDING_MANIFEST_COMMIT, expected_head],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        failures.append("frozen Finding Manifest commit is not an ancestor of the source candidate")
    return failures


def check_repository_identity(root: Path, identity: SourceIdentity, *, enforce_frozen_baseline: bool = True) -> tuple[list[str], list[str]]:
    """Validate a clean exact commit and return failures plus committed paths.

    ``enforce_frozen_baseline=False`` exists only for the temporary Git
    anti-bypass fixture; the production CLI always fixes the construction
    baseline to :data:`BASELINE`.
    """

    failures: list[str] = []
    if enforce_frozen_baseline and identity.baseline_head != BASELINE:
        failures.append(f"baseline is not frozen: expected {BASELINE}, got {identity.baseline_head}")
    if enforce_frozen_baseline and identity.branch != BRANCH:
        failures.append(f"branch parameter is not the Generation 2 branch: expected {BRANCH}, got {identity.branch}")
    try:
        if _git("cat-file", "-t", identity.baseline_head, root=root) != "commit":
            failures.append("baseline object is not a commit")
        if _git("cat-file", "-t", identity.expected_head, root=root) != "commit":
            failures.append("expected source object is not a commit")
    except RuntimeError as exc:
        return [f"commit identity cannot be resolved: {exc}"], []
    branch = _git("branch", "--show-current", root=root)
    head = _git("rev-parse", "HEAD", root=root)
    tree = _git("rev-parse", f"{identity.expected_head}^{{tree}}", root=root)
    if branch != identity.branch:
        failures.append(f"branch mismatch: expected {identity.branch}, got {branch}")
    if head != identity.expected_head:
        failures.append(f"HEAD mismatch: expected source {identity.expected_head}, got {head}")
    if tree != identity.expected_tree:
        failures.append(f"tree mismatch: expected {identity.expected_tree}, got {tree}")
    if _git("status", "--porcelain=v1", "--untracked-files=all", root=root):
        failures.append("working tree is not clean")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", identity.baseline_head, identity.expected_head],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        failures.append("frozen baseline is not an ancestor of the source candidate")
    if enforce_frozen_baseline:
        try:
            parents = _git("show", "-s", "--format=%P", identity.expected_head, root=root).split()
        except RuntimeError as exc:
            failures.append(f"Generation 2 parent cannot be resolved: {exc}")
        else:
            if parents != [GENERATION_PARENT]:
                failures.append(
                    "Generation 2 source must be a single commit with the exact generation parent: "
                    f"expected [{GENERATION_PARENT}], got {parents}"
                )
        for rejected in (REJECTED_SOURCE, REJECTED_EVIDENCE):
            try:
                if _git("cat-file", "-t", rejected, root=root) != "commit":
                    failures.append(f"rejected Generation 1 object is not a commit: {rejected}")
                    continue
            except RuntimeError as exc:
                failures.append(f"rejected Generation 1 object cannot be resolved: {rejected}: {exc}")
                continue
            rejected_ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", rejected, identity.expected_head],
                cwd=root,
                check=False,
                capture_output=True,
            )
            if rejected_ancestor.returncode == 0:
                failures.append(f"rejected Generation 1 commit is an ancestor of Generation 2: {rejected}")
            elif rejected_ancestor.returncode != 1:
                failures.append(f"rejected Generation 1 ancestry check failed: {rejected}")
    paths = committed_paths(root, identity.baseline_head, identity.expected_head)
    illegal = [path for path in paths if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)]
    if illegal:
        failures.append(f"write-set violation: {illegal}")
    if not paths:
        failures.append("source candidate has an empty baseline diff")
    if any("provider-openai-compatible" in path for path in paths):
        failures.append("forbidden openai-compatible provider path changed")
    if enforce_frozen_baseline:
        failures.extend(check_frozen_manifest_blob(root, identity.expected_head))
    return failures, paths


def check_catalog(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    path = root / "catalog" / "plugin-catalog-v1.json"
    if not path.exists():
        return [f"missing catalog: {path}"]
    catalog = _json(path)
    frozen_path = root / "governance" / "frozen-design-v1" / "plugin-catalog-v1.json"
    if not frozen_path.is_file():
        return [f"missing frozen catalog: {frozen_path}"]
    frozen_catalog = _json(frozen_path)
    try:
        verify_catalog(catalog)
    except Exception as exc:
        failures.append(f"catalog semantic validation failed: {exc}")
    if catalog.get("schema") != "novel-agent-plugin-catalog/v1":
        failures.append("catalog schema mismatch")
    entries = {item.get("plugin_id"): item for item in catalog.get("code_plugins", [])}
    frozen_entries = {item.get("plugin_id"): item for item in frozen_catalog.get("code_plugins", [])}
    anthropic = entries.get("com.plotpilot.novelagent.provider-anthropic")
    gemini = entries.get("com.plotpilot.novelagent.provider-gemini")
    duplicate = entries.get("com.plotpilot.novelagent.provider-openai-compatible")
    for folder, plugin_id, capability_id in PROVIDERS:
        name = folder.removeprefix("provider-")
        entry = entries.get(plugin_id)
        if entry != frozen_entries.get(plugin_id):
            failures.append(f"{name} provider entry differs from the frozen catalog")
        if not entry or entry.get("status") != "planned":
            failures.append(f"{name} provider is not planned in catalog")
            continue
        if entry.get("project") != "NAP-00" or entry.get("source_write_set") != [f"plugins/{folder}/**"]:
            failures.append(f"{name} provider source owner/write-set mismatch")
        if entry.get("needs") != PROVIDER_NEEDS:
            failures.append(f"{name} provider needs mismatch")
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, list) or len(capabilities) != 1:
            failures.append(f"{name} provider must expose exactly one capability")
            continue
        capability = capabilities[0]
        expected_capability = {
            "capability_id": capability_id,
            "operations": ["run", "cancel"],
            "result_contract": "artifact-bundle/v1",
            "input_schema": f"model.provider.{name}.invoke-request/v1",
            "output_schema": f"model.provider.{name}.invoke-result/v1",
            "deterministic": False,
            "accepted_data_formats": [],
            "ui_contributions": [],
            "headless": True,
        }
        if capability != expected_capability:
            failures.append(f"{name} catalog capability differs from the frozen descriptor projection")
    expected_duplicate = {
        "status": "not_planned_duplicate",
        "duplicate_of": "com.plotpilot.provider.openai-compatible",
        "project": None,
        "capabilities": [],
        "ui_slots": [],
        "needs": [],
    }
    if not duplicate or any(duplicate.get(key) != value for key, value in expected_duplicate.items()):
        failures.append("openai-compatible duplicate disposition is missing or changed")
    if duplicate != frozen_entries.get("com.plotpilot.novelagent.provider-openai-compatible"):
        failures.append("openai-compatible entry differs from the frozen catalog")
    return failures


def _package_files(package_root: Path, expected: dict[str, Any]) -> dict[str, bytes]:
    paths = expected.get("package_files")
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
        raise ValueError(f"invalid package_files inventory: {package_root}")
    return {path: (package_root / path).read_bytes() for path in paths}


def _read_lf_manifest(path: Path) -> bytes:
    data = path.read_bytes()
    if b"\r" in data or not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise ValueError(f"{path} must use LF and end with exactly one newline")
    data.decode("utf-8", "strict")
    return data


def _valid_wheel(path: Path, *, import_module: str | None = None) -> bool:
    if not path.is_file() or not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        wheel = [name for name in names if name.endswith(".dist-info/WHEEL")]
        records = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(metadata) != 1 or len(wheel) != 1 or len(records) != 1:
            return False
        wheel_text = archive.read(wheel[0]).decode("utf-8", "strict")
        if "Root-Is-Purelib: true" not in wheel_text or "Tag: py3-none-any" not in wheel_text:
            return False
        if import_module is not None and f"{import_module}.py" not in names and f"{import_module}/__init__.py" not in names:
            return False
    return True


def check_demos(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    for kind in ("code", "data", "skill"):
        package_root = root / "catalog" / "demos" / kind
        expected_path = package_root / "expected.json"
        manifest_path = package_root / ("skill.json" if kind == "skill" else "plugin.json")
        files_path = package_root / "files.sha256"
        if not all(path.is_file() for path in (expected_path, manifest_path, files_path)):
            failures.append(f"{kind} demo package is incomplete")
            continue
        try:
            expected = _json(expected_path)
            manifest = _json(manifest_path)
            package_files = _package_files(package_root, expected)
            manifest_bytes = _read_lf_manifest(files_path)
            if expected.get("files_sha256") != manifest_bytes.decode("utf-8"):
                failures.append(f"{kind} demo expected files.sha256 differs from the package manifest")
            if kind == "skill":
                verify_skill_identity(
                    package_files,
                    manifest["skill_id"],
                    manifest["version"],
                    expected["skill_package_hash"],
                    expected["skill_release_id"],
                    expected_files_sha256=manifest_bytes,
                )
                receipt = _json(package_root / "fixtures" / "skill-run-receipt.json")
                verify_skill_receipt(receipt)
                if receipt["package_hash"] != expected["skill_package_hash"] or receipt["release_id"] != expected["skill_release_id"]:
                    failures.append("Skill demo receipt identity mismatch")
            else:
                verify_manifest(manifest)
                verify_package_identity(
                    package_files,
                    manifest["plugin_id"],
                    manifest["version"],
                    expected["package_hash"],
                    expected["release_id"],
                    expected_files_sha256=manifest_bytes,
                )
                if kind == "data":
                    bundle = _json(package_root / "fixtures" / "plugin-data-bundle.json")
                    verify_data_bundle(bundle)
                    if bundle["package_hash"] != expected["package_hash"] or bundle["data_release_id"] != expected["release_id"]:
                        failures.append("Data demo bundle identity mismatch")
                else:
                    result = _json(package_root / "fixtures" / "result-bundle.json")
                    receipt = _json(package_root / "fixtures" / "provenance-receipt.json")
                    verify_result_bundle(result)
                    verify_provenance_receipt(receipt)
                    if result["producer"]["release_id"] != expected["release_id"] or receipt["package_hash"] != expected["package_hash"] or receipt["release_id"] != expected["release_id"]:
                        failures.append("Code demo result/receipt identity mismatch")
                    wheel = package_root / manifest["backend"]["wheel"]
                    entrypoint = str(manifest["backend"]["entrypoint"]).split(":", 1)[0]
                    if not _valid_wheel(wheel, import_module=entrypoint):
                        failures.append("Code demo Wheel is not a legal importable py3-none-any Wheel")
        except Exception as exc:
            failures.append(f"{kind} demo identity validation failed: {exc}")
    return failures


def _closed_schema(path: Path, expected_id: str) -> None:
    value = _json(path)
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError(f"{path} is not Draft 2020-12")
    if value.get("$id") != expected_id:
        raise ValueError(f"{path} does not publish descriptor schema ID {expected_id}")
    if value.get("type") != "object" or value.get("additionalProperties") is not False:
        raise ValueError(f"{path} is not a closed object schema")

    def require_closed_objects(node: Any, location: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                raise ValueError(f"{path} contains an open object schema at {location}")
            for key, child in node.items():
                require_closed_objects(child, f"{location}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                require_closed_objects(child, f"{location}[{index}]")

    require_closed_objects(value, "$")


def _verify_provider_schema_index(
    package_root: Path,
    module_name: str,
    plugin_id: str,
    capability_id: str,
    descriptor: dict[str, Any],
    expected: dict[str, Any],
    package_files: dict[str, bytes],
) -> None:
    """Bind each descriptor schema ID to one exact, hashed package artifact."""

    module_root = package_root / module_name
    index_path = module_root / "schemas" / "index.json"
    if not index_path.is_file():
        raise ValueError("provider schema index is missing")
    index = _json(index_path)
    if not isinstance(index, dict) or set(index) != {"schema", "plugin_id", "capability_id", "schemas"}:
        raise ValueError("provider schema index is not a closed provider-schema-index/v1 object")
    if (
        index.get("schema") != "provider-schema-index/v1"
        or index.get("plugin_id") != plugin_id
        or index.get("capability_id") != capability_id
    ):
        raise ValueError("provider schema index identity mismatch")
    entries = index.get("schemas")
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("provider schema index must contain exactly input and output schemas")

    expected_bindings: dict[str, str] = {}
    for descriptor_field, expected_field in (
        ("input_schema", "input_schema_path"),
        ("output_schema", "output_schema_path"),
    ):
        schema_id = descriptor.get(descriptor_field)
        package_relative = expected.get(expected_field)
        if not isinstance(schema_id, str) or not isinstance(package_relative, str):
            raise ValueError(f"provider schema index cannot bind {descriptor_field}")
        package_path = Path(package_relative)
        try:
            module_relative = package_path.relative_to(module_name).as_posix()
        except ValueError as exc:
            raise ValueError(f"{expected_field} is outside the provider module") from exc
        expected_bindings[schema_id] = module_relative

    indexed: dict[str, str] = {}
    indexed_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"schema_id", "path", "sha256"}:
            raise ValueError("provider schema index entry is not closed")
        schema_id = entry.get("schema_id")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(schema_id, str) or not isinstance(relative, str):
            raise ValueError("provider schema index entry identity is invalid")
        if schema_id in indexed or relative in indexed_paths:
            raise ValueError("provider schema index contains duplicate schema ID or path")
        if expected_bindings.get(schema_id) != relative:
            raise ValueError(f"provider schema index has an unexpected binding: {schema_id} -> {relative}")
        resolved = (module_root / relative).resolve()
        try:
            resolved.relative_to(module_root.resolve())
        except ValueError as exc:
            raise ValueError(f"provider schema index path escapes the module: {relative}") from exc
        if not resolved.is_file():
            raise ValueError(f"provider schema index artifact is missing: {relative}")
        actual_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if not isinstance(digest, str) or digest != actual_digest:
            raise ValueError(
                f"provider schema index digest mismatch for {schema_id}: expected {digest}, actual {actual_digest}"
            )
        indexed[schema_id] = relative
        indexed_paths.add(relative)
        package_relative = resolved.relative_to(package_root.resolve()).as_posix()
        if package_relative not in package_files:
            raise ValueError(f"provider schema index artifact is outside the hashed package: {package_relative}")

    if indexed != expected_bindings:
        raise ValueError(f"provider schema index bindings differ: expected {expected_bindings}, got {indexed}")
    physical_schema_paths = {
        path.relative_to(module_root).as_posix()
        for path in (module_root / "schemas").rglob("*.schema.json")
        if path.is_file()
    }
    if physical_schema_paths != set(expected_bindings.values()):
        raise ValueError(
            "provider package does not contain exactly the indexed schema artifacts: "
            f"expected {sorted(expected_bindings.values())}, got {sorted(physical_schema_paths)}"
        )


def check_provider_descriptors(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    catalog = _json(root / "catalog" / "plugin-catalog-v1.json")
    entries = {entry.get("plugin_id"): entry for entry in catalog.get("code_plugins", [])}
    for folder, plugin_id, capability_id in PROVIDERS:
        package_root = root / "plugins" / folder
        if not package_root.exists():
            failures.append(f"missing provider directory: {package_root}")
            continue
        expected_path = package_root / "expected.json"
        manifest_path = package_root / "plugin.json"
        descriptor_path = package_root / "descriptor.json"
        files_path = package_root / "files.sha256"
        if not all(path.is_file() for path in (expected_path, manifest_path, descriptor_path, files_path)):
            failures.append(f"{plugin_id} package identity files are incomplete")
            continue
        try:
            expected = _json(expected_path)
            manifest = _json(manifest_path)
            descriptor = _json(descriptor_path)
            package_files = _package_files(package_root, expected)
            actual_package_paths = {
                path.relative_to(package_root).as_posix()
                for path in package_root.rglob("*")
                if path.is_file()
                and path.name != "expected.json"
                and path.name != ".gitattributes"
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            }
            if set(package_files) != actual_package_paths:
                missing = sorted(actual_package_paths - set(package_files))
                extra = sorted(set(package_files) - actual_package_paths)
                failures.append(f"{plugin_id} package inventory is incomplete: missing={missing}, extra={extra}")
            files_bytes = _read_lf_manifest(files_path)
            if expected.get("files_sha256") != files_bytes.decode("utf-8"):
                failures.append(f"{plugin_id} expected files.sha256 differs from the package manifest")
            manifest_names = expected.get("manifest_files")
            identity_names = expected.get("identity_files")
            if (
                not isinstance(manifest_names, list)
                or not manifest_names
                or any(not isinstance(path, str) for path in manifest_names)
                or set(manifest_names) != set(package_files) - {"files.sha256"}
            ):
                raise ValueError("provider manifest_files does not cover the physical package")
            if (
                not isinstance(identity_names, list)
                or not identity_names
                or any(not isinstance(path, str) for path in identity_names)
                or not set(identity_names) <= set(manifest_names)
            ):
                raise ValueError("provider identity_files is not a closed subset of manifest_files")
            manifest_files = {path: package_files[path] for path in manifest_names}
            identity_files = {path: package_files[path] for path in identity_names}
            verify_package_manifest(manifest_files, files_bytes)
            verify_manifest(manifest)
            verify_package_identity(
                identity_files,
                plugin_id,
                manifest["version"],
                expected["package_hash"],
                expected["release_id"],
            )
            verify_capability_descriptor(
                descriptor,
                expected_capability_id=capability_id,
                allowed_capability_ids={capability_id},
                expected_provider={"plugin_id": plugin_id, "release_id": expected["release_id"]},
            )
            if manifest.get("needs") != PROVIDER_NEEDS or entries.get(plugin_id, {}).get("needs") != PROVIDER_NEEDS:
                failures.append(f"{plugin_id} needs differ across package/catalog")
            catalog_capability = entries.get(plugin_id, {}).get("capabilities", [None])[0]
            if not isinstance(catalog_capability, dict):
                failures.append(f"{plugin_id} has no catalog capability")
            else:
                descriptor_projection = {
                    key: descriptor[key]
                    for key in ("capability_id", "input_schema", "output_schema", "result_contract", "deterministic", "accepted_data_formats")
                }
                catalog_projection = {
                    key: catalog_capability[key]
                    for key in ("capability_id", "input_schema", "output_schema", "result_contract", "deterministic", "accepted_data_formats")
                }
                if descriptor_projection != catalog_projection or descriptor.get("supports") != catalog_capability.get("operations"):
                    failures.append(f"{plugin_id} descriptor differs from catalog capability")
            input_schema_path = package_root / str(expected["input_schema_path"])
            output_schema_path = package_root / str(expected["output_schema_path"])
            module_name = folder.replace("-", "_")
            _verify_provider_schema_index(
                package_root,
                module_name,
                plugin_id,
                capability_id,
                descriptor,
                expected,
                package_files,
            )
            _closed_schema(input_schema_path, descriptor["input_schema"])
            _closed_schema(output_schema_path, descriptor["output_schema"])
            for path in (str(expected["input_schema_path"]), str(expected["output_schema_path"])):
                if path not in package_files:
                    failures.append(f"{plugin_id} schema is outside the hashed package inventory: {path}")
            wheel_path = package_root / str(expected.get("wheel_path") or manifest["backend"]["wheel"])
            import_module = str(manifest["backend"]["entrypoint"]).split(":", 1)[0].split(".", 1)[0]
            if not _valid_wheel(wheel_path, import_module=import_module):
                failures.append(f"{plugin_id} Wheel is not a legal importable py3-none-any Wheel")
            for section, field in (("backend", "wheel"), ("backend", "requirements_lock"), ("backend", "wheelhouse"), ("storage", "migration_manifest")):
                value = manifest[section][field]
                referenced = package_root / value
                if not referenced.exists():
                    failures.append(f"{plugin_id} manifest reference is missing: {value}")
        except Exception as exc:
            failures.append(f"{plugin_id} package/descriptor validation failed: {exc}")

        # No alternate descriptor hidden elsewhere may shadow the canonical
        # descriptor bound above.
        candidates = []
        for path in package_root.rglob("*.json"):
            try:
                value = _json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("plugin_id") == plugin_id or value.get("capability_id") == capability_id:
                candidates.append(value)
        if not candidates:
            failures.append(f"missing descriptor for {plugin_id}")
            continue
        canonical_candidates = [item for item in candidates if item.get("schema") == "capability-provider/v1" and item.get("capability_id") == capability_id]
        if len(canonical_candidates) != 1:
            failures.append(f"{plugin_id} must have exactly one canonical descriptor")
        failure_fixture = root / "tests" / "providers" / "fixtures" / ("anthropic-failure.json" if folder == "provider-anthropic" else "gemini-failure.json")
        if failure_fixture.exists():
            failures_value = _json(failure_fixture)
            if not failures_value or not isinstance(failures_value, list) or not any(isinstance(item, dict) and ("error" in item or "promptFeedback" in item) for item in failures_value):
                failures.append(f"{plugin_id} conditional failure fixture is missing")
    return failures


def check_typescript_compile(root: Path = ROOT) -> list[str]:
    package_root = root / "sdk" / "typescript"
    package_path = package_root / "package.json"
    lock_path = package_root / "package-lock.json"
    if not package_path.is_file() or not lock_path.is_file():
        return ["TypeScript package.json/package-lock.json is incomplete"]
    try:
        package = _json(package_path)
        lock = _json(lock_path)
        version = package.get("devDependencies", {}).get("typescript")
        if not isinstance(version, str) or not version or version[0] in "^~><=*":
            return ["TypeScript compiler version is not exact"]
        lock_version = lock.get("packages", {}).get("node_modules/typescript", {}).get("version")
        root_version = lock.get("packages", {}).get("", {}).get("devDependencies", {}).get("typescript")
        if lock_version != version or root_version != version:
            return ["TypeScript package-lock does not pin the declared compiler version"]
        if package.get("scripts", {}).get("typecheck") != "tsc --noEmit -p tsconfig.json":
            return ["TypeScript typecheck script is not the frozen noEmit command"]
    except Exception as exc:
        return [f"TypeScript compiler lock validation failed: {exc}"]
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        return ["npm is unavailable for the locked TypeScript compile"]
    installed = subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--fund=false"],
        cwd=package_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_UPDATE_NOTIFIER": "1"},
    )
    if installed.returncode:
        detail = (installed.stdout + "\n" + installed.stderr).strip()
        return [f"locked TypeScript npm ci failed: {detail}"]
    completed = subprocess.run(
        [npm, "run", "typecheck", "--silent"],
        cwd=package_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_UPDATE_NOTIFIER": "1"},
    )
    if completed.returncode:
        detail = (completed.stdout + "\n" + completed.stderr).strip()
        return [f"locked tsc --noEmit failed: {detail}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-head", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    identity = SourceIdentity(args.baseline_head, args.expected_head, args.expected_tree, args.branch)
    repository_failures, paths = check_repository_identity(ROOT, identity)
    failures = (
        repository_failures
        + check_catalog()
        + check_demos()
        + check_provider_descriptors()
        + check_typescript_compile()
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "baseline_head": identity.baseline_head,
                "branch": _git("branch", "--show-current"),
                "source_head": _git("rev-parse", "HEAD"),
                "source_tree": _git("rev-parse", "HEAD^{tree}"),
                "changed_paths": paths,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
