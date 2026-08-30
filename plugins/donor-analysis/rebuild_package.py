"""Public deterministic outer package rebuild driver."""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from donor_analysis.build_backend import rebuild_package  # noqa: E402

def main() -> None:
    value = rebuild_package()
    print(json.dumps({key: value[key] for key in ("plugin_id", "package_hash", "release_id", "wheel_path")}, sort_keys=True))

if __name__ == "__main__":
    main()
