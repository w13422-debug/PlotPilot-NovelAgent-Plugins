"""Minimal NAP-00 B0 delivery gate.

The gate is deliberately a control-plane checker: it validates repository
identity, write-set containment, catalog decisions and the presence of the
three deterministic demo package classes and both native provider descriptors.
It never merges, touches a donor repository, starts the application, or makes
a Sol review decision.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from plotpilot_plugin_sdk.verifier import verify_catalog  # noqa: E402
BASELINE = "c1b9519c7d25ce1fbef07984cdb548c89e7e1152"
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


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def changed_paths(base: str) -> list[str]:
    paths: set[str] = set()
    for args in (("diff", "--name-only", f"{base}..HEAD"), ("diff", "--name-only"), ("diff", "--cached", "--name-only")):
        paths.update(p for p in _git(*args).splitlines() if p)
    # Porcelain reports an untracked directory as one abbreviated path (for
    # example ``plugins/``), which is not enough to enforce a file-level
    # write-set.  Ask Git for the complete untracked file list instead; this
    # also honours the repository's ignore rules.
    paths.update(p for p in _git("ls-files", "--others", "--exclude-standard").splitlines() if p)
    return sorted(paths)


def check_repository(args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    branch = _git("branch", "--show-current")
    head = _git("rev-parse", "HEAD")
    if branch != args.branch:
        failures.append(f"branch mismatch: expected {args.branch}, got {branch}")
    if args.expected_head and head != args.expected_head:
        failures.append(f"HEAD mismatch: expected {args.expected_head}, got {head}")
    paths = changed_paths(args.base_head)
    illegal = [path for path in paths if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)]
    if illegal:
        failures.append(f"write-set violation: {illegal}")
    if args.require_changes and not paths:
        failures.append("no B0 changes detected")
    if args.clean and _git("status", "--porcelain"):
        failures.append("working tree is not clean")
    if any("provider-openai-compatible" in path for path in paths):
        failures.append("forbidden openai-compatible provider path changed")
    return failures


def check_catalog() -> list[str]:
    failures: list[str] = []
    path = ROOT / "catalog" / "plugin-catalog-v1.json"
    if not path.exists():
        return [f"missing catalog: {path}"]
    catalog = _json(path)
    try:
        verify_catalog(catalog)
    except Exception as exc:
        failures.append(f"catalog semantic validation failed: {exc}")
    if catalog.get("schema") != "novel-agent-plugin-catalog/v1":
        failures.append("catalog schema mismatch")
    entries = {item.get("plugin_id"): item for item in catalog.get("code_plugins", [])}
    anthropic = entries.get("com.plotpilot.novelagent.provider-anthropic")
    gemini = entries.get("com.plotpilot.novelagent.provider-gemini")
    duplicate = entries.get("com.plotpilot.novelagent.provider-openai-compatible")
    for name, entry in (("anthropic", anthropic), ("gemini", gemini)):
        if not entry or entry.get("status") != "planned":
            failures.append(f"{name} provider is not planned in catalog")
        if entry and not entry.get("capabilities"):
            failures.append(f"{name} provider has no capability descriptor")
    if not duplicate or duplicate.get("status") != "not_planned_duplicate":
        failures.append("openai-compatible duplicate disposition is missing or changed")
    return failures


def _manifest_files() -> list[Path]:
    return sorted(path for root in (ROOT / "catalog", ROOT / "plugins") if root.exists() for path in root.rglob("*.json"))


def check_demos() -> list[str]:
    failures: list[str] = []
    manifests = []
    for path in _manifest_files():
        try:
            value = _json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("schema") in {"plotpilot-plugin/v1", "plotpilot-skill/v1"}:
            manifests.append((path, value))
    kinds = {value.get("kind") for _, value in manifests if value.get("schema") == "plotpilot-plugin/v1"}
    if "code" not in kinds:
        failures.append("no Code demo package manifest found")
    if "data" not in kinds:
        failures.append("no Data demo package manifest found")
    if not any(value.get("schema") == "plotpilot-skill/v1" for _, value in manifests):
        failures.append("no Skill demo package manifest found")
    for path, value in manifests:
        if not any(part.lower() in {"demos", "demo", "golden"} for part in path.parts):
            continue
        package_dir = path.parent
        if not (package_dir / "files.sha256").exists():
            failures.append(f"demo package missing files.sha256: {path}")
    return failures


def check_provider_descriptors() -> list[str]:
    failures: list[str] = []
    expected = {
        "com.plotpilot.novelagent.provider-anthropic": "model.provider.anthropic.invoke/v1",
        "com.plotpilot.novelagent.provider-gemini": "model.provider.gemini.invoke/v1",
    }
    for folder, plugin_id, capability_id in (
        ("provider-anthropic", "com.plotpilot.novelagent.provider-anthropic", "model.provider.anthropic.invoke/v1"),
        ("provider-gemini", "com.plotpilot.novelagent.provider-gemini", "model.provider.gemini.invoke/v1"),
    ):
        root = ROOT / "plugins" / folder
        if not root.exists():
            failures.append(f"missing provider directory: {root}")
            continue
        candidates = []
        for path in root.rglob("*.json"):
            try:
                value = _json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("plugin_id") == plugin_id or value.get("capability_id") == capability_id:
                candidates.append(value)
        if not candidates:
            failures.append(f"missing descriptor for {plugin_id}")
            continue
        descriptor = next((item for item in candidates if item.get("capability_id") == capability_id), candidates[0])
        catalog_entry = next((item for item in _json(ROOT / "catalog" / "plugin-catalog-v1.json").get("code_plugins", []) if item.get("plugin_id") == plugin_id), None)
        if not catalog_entry or catalog_entry.get("project") != "NAP-00" or catalog_entry.get("source_write_set") != [f"plugins/{folder}/**"]:
            failures.append(f"{plugin_id} source owner/write-set is not NAP-00")
        if catalog_entry and catalog_entry.get("needs") != _json(root / "plugin.json").get("needs"):
            failures.append(f"{plugin_id} needs differ from catalog")
        if descriptor.get("result_contract") != "artifact-bundle/v1":
            failures.append(f"{plugin_id} descriptor result contract mismatch")
        if descriptor.get("provider", {}).get("plugin_id") != plugin_id:
            failures.append(f"{plugin_id} descriptor provider owner mismatch")
        if set(descriptor.get("supports", descriptor.get("operations", []))) != {"run", "cancel"}:
            failures.append(f"{plugin_id} descriptor supports must be run/cancel")
        if descriptor.get("accepted_data_formats", []) != []:
            failures.append(f"{plugin_id} descriptor must accept no Data formats")
        failure_fixture = ROOT / "tests" / "providers" / "fixtures" / ("anthropic-failure.json" if folder == "provider-anthropic" else "gemini-failure.json")
        if failure_fixture.exists():
            failures_value = _json(failure_fixture)
            if not failures_value or not isinstance(failures_value, list) or not any(isinstance(item, dict) and ("error" in item or "promptFeedback" in item) for item in failures_value):
                failures.append(f"{plugin_id} conditional failure fixture is missing")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-head", default=BASELINE)
    parser.add_argument("--expected-head")
    parser.add_argument("--branch", default="codex/nap-00-integration")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--require-changes", action="store_true")
    args = parser.parse_args(argv)
    failures = check_repository(args) + check_catalog() + check_demos() + check_provider_descriptors()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(json.dumps({"status": "ok", "branch": _git("branch", "--show-current"), "head": _git("rev-parse", "HEAD"), "changed_paths": changed_paths(args.base_head)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
