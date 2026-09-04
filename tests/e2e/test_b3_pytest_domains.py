from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tools" / "integration" / "run_b3_pytest_domains.py"
DOMAINS = ("narrative", "character", "style", "contracts")
REAL_RUN = subprocess.run


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("nap00_b3_domain_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


RUNNER = _load_runner()


def _expected_command(domain: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"tests/{domain}",
    ]


def _expected_output(exits: list[int]) -> list[str]:
    return [
        (
            f"domain={domain} "
            f"command={json.dumps(_expected_command(domain), ensure_ascii=False, separators=(',', ':'))} "
            f"raw_exit_code={exit_code}"
        )
        for domain, exit_code in zip(DOMAINS, exits, strict=False)
    ]


def _run_fake_pytest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    exits: list[int],
    *,
    fail_fast: bool = False,
) -> tuple[int, list[dict[str, object]], str]:
    log_path = tmp_path / "fake-pytest.jsonl"
    expected_exits = dict(zip(DOMAINS, exits, strict=True))
    parent_marker = "inherited-from-parent"
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", parent_marker)
    monkeypatch.setenv("B3_FAKE_PARENT_MARKER", parent_marker)

    fake_pytest_code = (
        "import json, os, pathlib, sys\n"
        "record_path = pathlib.Path(sys.argv[1])\n"
        "record = json.loads(sys.argv[2])\n"
        "record.update({\n"
        "    'pid': os.getpid(),\n"
        "    'cwd': os.getcwd(),\n"
        "    'PYTHONDONTWRITEBYTECODE': os.environ.get('PYTHONDONTWRITEBYTECODE'),\n"
        "    'B3_FAKE_PARENT_MARKER': os.environ.get('B3_FAKE_PARENT_MARKER'),\n"
        "})\n"
        "with record_path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(record, sort_keys=True) + '\\n')\n"
        "raise SystemExit(int(sys.argv[3]))\n"
    )

    def fake_pytest_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        domain = command[-1].removeprefix("tests/")
        child_record = {"argv": command, "domain": domain}
        child = REAL_RUN(
            [
                sys.executable,
                "-c",
                fake_pytest_code,
                str(log_path),
                json.dumps(child_record, separators=(",", ":")),
                str(expected_exits[domain]),
            ],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return subprocess.CompletedProcess(command, child.returncode)

    monkeypatch.setattr(RUNNER.subprocess, "run", fake_pytest_run)
    monkeypatch.chdir(tmp_path)
    runner_args = ["--fail-fast"] if fail_fast else []
    result = RUNNER.main(runner_args)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    output = capsys.readouterr().out
    return result, records, output


def test_domains_use_independent_processes_and_fixed_single_root_invocations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result, records, output = _run_fake_pytest(monkeypatch, capsys, tmp_path, [0, 0, 0, 0])

    assert result == 0
    assert [record["domain"] for record in records] == list(DOMAINS)
    assert len({record["pid"] for record in records}) == len(DOMAINS)
    assert [record["argv"] for record in records] == [_expected_command(domain) for domain in DOMAINS]
    assert all(Path(record["cwd"]).resolve() == ROOT for record in records)
    assert {record["PYTHONDONTWRITEBYTECODE"] for record in records} == {
        "inherited-from-parent"
    }
    assert {record["B3_FAKE_PARENT_MARKER"] for record in records} == {"inherited-from-parent"}
    assert output.splitlines() == _expected_output([0, 0, 0, 0])


def test_aggregate_runs_all_domains_and_returns_first_nonzero_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result, records, output = _run_fake_pytest(monkeypatch, capsys, tmp_path, [0, 2, 1, 5])

    assert result == 2
    assert [record["domain"] for record in records] == list(DOMAINS)
    assert output.splitlines() == _expected_output([0, 2, 1, 5])


def test_fail_fast_stops_after_first_failed_domain_and_preserves_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result, records, output = _run_fake_pytest(
        monkeypatch, capsys, tmp_path, [0, 2, 0, 0], fail_fast=True
    )

    assert result == 2
    assert [record["domain"] for record in records] == ["narrative", "character"]
    assert output.splitlines() == _expected_output([0, 2])


@pytest.mark.parametrize("exit_code", range(1, 6))
def test_original_pytest_exit_codes_are_propagated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    exit_code: int,
) -> None:
    exits = [exit_code, 0, 0, 0]
    result, records, output = _run_fake_pytest(monkeypatch, capsys, tmp_path, exits)

    assert result == exit_code
    assert len(records) == len(DOMAINS)
    assert output.splitlines() == _expected_output(exits)


@pytest.mark.parametrize("argv", [("tests/narrative",), ("--import-mode=importlib",)])
def test_runner_rejects_caller_supplied_roots_and_import_mode(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as raised:
        RUNNER.main(list(argv))

    assert raised.value.code == 2
