from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

GATE_PATH = ROOT / "tools" / "integration" / "validate_b0_delivery.py"
SPEC = importlib.util.spec_from_file_location("nap00_b0_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def strict_git_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", gate.BRANCH)
    _git(repo, "config", "user.name", "NAP-00 fixture")
    _git(repo, "config", "user.email", "nap00-fixture@example.invalid")
    manifest = repo / gate.FINDING_MANIFEST_PATH
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes((ROOT / gate.FINDING_MANIFEST_PATH).read_bytes())
    (repo / "contracts").mkdir()
    (repo / "contracts" / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    baseline = _commit(repo, "baseline")
    (repo / "contracts" / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    tree = _git(repo, "rev-parse", f"{candidate}^{{tree}}")
    return repo, baseline, candidate, tree


def _identity(baseline: str, candidate: str, tree: str) -> object:
    return gate.SourceIdentity(baseline, candidate, tree, gate.BRANCH)


def test_exact_source_repository_gate_accepts_only_the_clean_commit_tree(
    strict_git_fixture: tuple[Path, str, str, str]
) -> None:
    repo, baseline, candidate, tree = strict_git_fixture
    failures, paths = gate.check_repository_identity(
        repo, _identity(baseline, candidate, tree), enforce_frozen_baseline=False
    )
    assert failures == []
    assert paths == ["contracts/candidate.txt"]

    failures, _ = gate.check_repository_identity(repo, _identity(baseline, candidate, tree))
    assert any("baseline is not frozen" in failure for failure in failures)

    (repo / "contracts" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    failures, _ = gate.check_repository_identity(
        repo, _identity(baseline, candidate, tree), enforce_frozen_baseline=False
    )
    assert any("not clean" in failure for failure in failures)


def test_repository_gate_rejects_evidence_head_wrong_tree_empty_diff_and_illegal_path(
    strict_git_fixture: tuple[Path, str, str, str]
) -> None:
    repo, baseline, candidate, tree = strict_git_fixture
    (repo / "docs" / "deliveries").mkdir(parents=True)
    (repo / "docs" / "deliveries" / "evidence.md").write_text("evidence\n", encoding="utf-8")
    evidence = _commit(repo, "evidence")

    failures, _ = gate.check_repository_identity(
        repo, _identity(baseline, candidate, tree), enforce_frozen_baseline=False
    )
    assert any("HEAD mismatch" in failure for failure in failures)

    evidence_tree = _git(repo, "rev-parse", f"{evidence}^{{tree}}")
    failures, _ = gate.check_repository_identity(
        repo, _identity(baseline, evidence, "0" * 40), enforce_frozen_baseline=False
    )
    assert any("tree mismatch" in failure for failure in failures)

    failures, _ = gate.check_repository_identity(
        repo, _identity(evidence, evidence, evidence_tree), enforce_frozen_baseline=False
    )
    assert any("empty baseline diff" in failure for failure in failures)

    (repo / "outside.txt").write_text("illegal\n", encoding="utf-8")
    illegal = _commit(repo, "illegal")
    illegal_tree = _git(repo, "rev-parse", f"{illegal}^{{tree}}")
    failures, _ = gate.check_repository_identity(
        repo, _identity(baseline, illegal, illegal_tree), enforce_frozen_baseline=False
    )
    assert any("write-set violation" in failure for failure in failures)


def test_frozen_manifest_identity_uses_committed_blob_not_checkout_bytes() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    assert gate.check_frozen_manifest_blob(ROOT, head) == []
    committed = subprocess.run(
        ["git", "cat-file", "blob", f"{gate.FINDING_MANIFEST_COMMIT}:{gate.FINDING_MANIFEST_PATH}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    assert len(committed) == gate.FINDING_MANIFEST_SIZE == 12646
    assert hashlib.sha256(committed).hexdigest() == gate.FINDING_MANIFEST_SHA256
    worktree_hash = hashlib.sha256((ROOT / gate.FINDING_MANIFEST_PATH).read_bytes()).hexdigest()
    assert worktree_hash in {
        gate.FINDING_MANIFEST_SHA256,
        gate.FINDING_MANIFEST_PRECOMMIT_WORKTREE_SHA256,
    }


def test_generation_two_source_has_exact_parent_and_excludes_rejected_generation() -> None:
    source_head = _source_candidate_from_current_history()
    assert _git(ROOT, "show", "-s", "--format=%P", source_head).split() == [gate.GENERATION_PARENT]
    for rejected in (gate.REJECTED_SOURCE, gate.REJECTED_EVIDENCE):
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", rejected, source_head],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 1, rejected


def _copy_delivery_tree(tmp_path: Path) -> Path:
    copied = tmp_path / "delivery"
    for relative in ("catalog", "plugins", "tests/providers"):
        source = ROOT / relative
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    frozen_catalog = "governance/frozen-design-v1/plugin-catalog-v1.json"
    target = copied / frozen_catalog
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / frozen_catalog, target)
    return copied


def test_strict_content_gate_accepts_catalog_demos_and_provider_identities() -> None:
    assert gate.check_catalog(ROOT) == []
    assert gate.check_demos(ROOT) == []
    assert gate.check_provider_descriptors(ROOT) == []
    assert gate.check_typescript_compile(ROOT) == []


@pytest.mark.parametrize(
    "mutation,checker",
    [
        ("missing_code_demo", "demos"),
        ("tampered_files_manifest", "demos"),
        ("invalid_wheel", "demos"),
        ("deleted_duplicate_of", "catalog"),
        ("wrong_need", "catalog"),
        ("deleted_descriptor_field", "providers"),
        ("provider_owner_drift", "catalog"),
        ("provider_authority_drift", "catalog"),
    ],
)
def test_delivery_content_mutations_fail_closed(tmp_path: Path, mutation: str, checker: str) -> None:
    copied = _copy_delivery_tree(tmp_path)
    catalog_path = copied / "catalog" / "plugin-catalog-v1.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = {entry["plugin_id"]: entry for entry in catalog["code_plugins"]}

    if mutation == "missing_code_demo":
        shutil.rmtree(copied / "catalog" / "demos" / "code")
    elif mutation == "tampered_files_manifest":
        path = copied / "catalog" / "demos" / "code" / "files.sha256"
        path.write_bytes(path.read_bytes() + b"0" * 64 + b"  injected.txt\n")
    elif mutation == "invalid_wheel":
        expected = json.loads((copied / "catalog" / "demos" / "code" / "expected.json").read_text(encoding="utf-8"))
        wheel = next(path for path in expected["package_files"] if path.endswith(".whl") and "/wheels/" not in path)
        (copied / "catalog" / "demos" / "code" / wheel).write_bytes(b"not-a-wheel")
    elif mutation == "deleted_duplicate_of":
        entries["com.plotpilot.novelagent.provider-openai-compatible"].pop("duplicate_of")
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    elif mutation == "wrong_need":
        entries["com.plotpilot.novelagent.provider-anthropic"]["needs"][0] = "host.log/v1"
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    elif mutation == "deleted_descriptor_field":
        path = copied / "plugins" / "provider-gemini" / "descriptor.json"
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        descriptor.pop("output_schema")
        path.write_text(json.dumps(descriptor), encoding="utf-8")
    elif mutation == "provider_owner_drift":
        entries["com.plotpilot.novelagent.provider-gemini"]["project"] = "NAP-01"
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    elif mutation == "provider_authority_drift":
        entries["com.plotpilot.novelagent.provider-gemini"]["authority"] = "caller-owned"
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    failures = {
        "catalog": gate.check_catalog,
        "demos": gate.check_demos,
        "providers": gate.check_provider_descriptors,
    }[checker](copied)
    assert failures, f"mutation bypassed {checker}: {mutation}"


@pytest.mark.parametrize("mutation", ["stale_index_digest", "schema_byte_drift", "unindexed_schema"])
@pytest.mark.parametrize("folder", ["provider-anthropic", "provider-gemini"])
def test_provider_schema_index_binds_exact_unique_artifacts(
    tmp_path: Path, folder: str, mutation: str
) -> None:
    copied = _copy_delivery_tree(tmp_path)
    package_root = copied / "plugins" / folder
    module_name = folder.replace("-", "_")
    expected = json.loads((package_root / "expected.json").read_text(encoding="utf-8"))
    descriptor = json.loads((package_root / "descriptor.json").read_text(encoding="utf-8"))
    package_files = gate._package_files(package_root, expected)
    index_path = package_root / module_name / "schemas" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))

    if mutation == "stale_index_digest":
        index["schemas"][0]["sha256"] = "0" * 64
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    elif mutation == "schema_byte_drift":
        schema_path = package_root / module_name / index["schemas"][0]["path"]
        schema_path.write_bytes(schema_path.read_bytes() + b"\n")
    else:
        extra = package_root / module_name / "schemas" / "shadow.schema.json"
        extra.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="provider schema index|exactly the indexed schema artifacts"):
        gate._verify_provider_schema_index(
            package_root,
            module_name,
            descriptor["provider"]["plugin_id"],
            descriptor["capability_id"],
            descriptor,
            expected,
            package_files,
        )


@pytest.mark.parametrize(
    "relative,checker,mutation",
    [
        ("catalog/demos/code/files.sha256", gate.check_demos, "crlf"),
        ("catalog/demos/data/files.sha256", gate.check_demos, "missing-newline"),
        ("plugins/provider-anthropic/files.sha256", gate.check_provider_descriptors, "crlf"),
        ("plugins/provider-gemini/files.sha256", gate.check_provider_descriptors, "missing-newline"),
    ],
)
def test_package_manifests_reject_crlf_and_missing_final_lf(
    tmp_path: Path, relative: str, checker: object, mutation: str
) -> None:
    copied = _copy_delivery_tree(tmp_path)
    path = copied / relative
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    path.write_bytes(raw.replace(b"\n", b"\r\n") if mutation == "crlf" else raw.rstrip(b"\n"))
    assert checker(copied), f"{mutation} package manifest bypassed {relative}"  # type: ignore[operator]


def _source_candidate_from_current_history() -> str:
    # Downstream no-ff integrations make HEAD (and HEAD^) unrelated to the
    # frozen B0 Generation-2 source. Locate that source by its immutable
    # single-parent boundary instead of guessing from the latest commit.
    candidates = []
    for commit in _git(ROOT, "rev-list", "--topo-order", "HEAD").splitlines():
        parents = _git(ROOT, "show", "-s", "--format=%P", commit).split()
        if parents == [gate.GENERATION_PARENT] and commit not in {
            gate.REJECTED_SOURCE,
            gate.REJECTED_EVIDENCE,
        }:
            candidates.append(commit)
    assert len(candidates) == 1, f"expected one accepted B0 G2 source, got {candidates}"
    return candidates[0]


def test_exact_source_gate_passes_fresh_checkout_with_windows_autocrlf(tmp_path: Path) -> None:
    source_head = _source_candidate_from_current_history()
    source_tree = _git(ROOT, "rev-parse", f"{source_head}^{{tree}}")
    checkout = tmp_path / "fresh-autocrlf"
    subprocess.run(
        ["git", "clone", "--no-local", "--no-checkout", str(ROOT), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(checkout, "config", "core.autocrlf", "true")
    _git(checkout, "checkout", "-B", gate.BRANCH, source_head)
    assert _git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
    for relative in (
        "catalog/demos/code/files.sha256",
        "catalog/demos/data/files.sha256",
        "catalog/demos/skill/files.sha256",
        "plugins/provider-anthropic/files.sha256",
        "plugins/provider-gemini/files.sha256",
    ):
        raw = (checkout / relative).read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n"), relative
    completed = subprocess.run(
        [
            sys.executable,
            str(checkout / "tools" / "integration" / "validate_b0_delivery.py"),
            "--baseline-head",
            gate.BASELINE,
            "--expected-head",
            source_head,
            "--expected-tree",
            source_tree,
            "--branch",
            gate.BRANCH,
        ],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, f"stdout={completed.stdout}\nstderr={completed.stderr}"
    assert '"status": "ok"' in completed.stdout


def test_gate_cli_requires_all_exact_source_identity_parameters() -> None:
    completed = subprocess.run(
        [sys.executable, str(GATE_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode != 0
    for parameter in ("--baseline-head", "--expected-head", "--expected-tree", "--branch"):
        assert parameter in completed.stderr


def test_public_sdk_validates_fixture_surface() -> None:
    from plotpilot_plugin_sdk.verifier import validate_contract

    schema_by_fixture = {
        "capability-provider.json": "capability-provider/v1",
        "plugin-data-bundle.json": "plugin-data-bundle/v1",
        "plugin-generation.json": "plugin-generation/v1",
        "plugin-lifecycle-transition.json": "plugin-lifecycle-transition/v1",
        "plugin-manifest-code.json": "plugin-manifest/v1",
        "plugin-manifest-data.json": "plugin-manifest/v1",
        "plugin-plan.json": "plugin-plan/v1",
        "skill-manifest.json": "skill-manifest/v1",
    }
    fixture_dir = ROOT / "contracts" / "examples" / "fixtures"
    for filename, contract_id in schema_by_fixture.items():
        value = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))
        assert validate_contract(contract_id, value) == [], filename


def test_plan_rejects_legacy_compare_mode() -> None:
    from plotpilot_plugin_sdk.errors import ContractValidationError
    from plotpilot_plugin_sdk.verifier import verify_plan

    plan = json.loads((ROOT / "contracts" / "examples" / "fixtures" / "plugin-plan.json").read_text(encoding="utf-8"))
    invalid = {**plan, "result_mode": "compare"}
    with pytest.raises(ContractValidationError):
        verify_plan(invalid)
