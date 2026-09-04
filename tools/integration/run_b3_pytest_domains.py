from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ("narrative", "character", "style", "contracts")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the B3 pytest domains in isolated subprocesses.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first domain with a non-zero pytest exit code",
    )
    return parser.parse_args(argv)


def _command_for(domain: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"tests/{domain}",
    ]


def _run_domain(domain: str, environment: dict[str, str]) -> int:
    command = _command_for(domain)
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    raw_exit_code = completed.returncode
    rendered_command = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    print(
        f"domain={domain} command={rendered_command} raw_exit_code={raw_exit_code}",
        flush=True,
    )
    return raw_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    environment = os.environ.copy()
    first_failure = 0

    for domain in DOMAINS:
        raw_exit_code = _run_domain(domain, environment)
        if raw_exit_code != 0:
            if first_failure == 0:
                first_failure = raw_exit_code
            if args.fail_fast:
                return raw_exit_code

    return first_failure


if __name__ == "__main__":
    raise SystemExit(main())
