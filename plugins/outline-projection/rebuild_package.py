"""Rebuild and verify the deterministic outline projection package."""
from __future__ import annotations
import json
from outline_projection.build_backend import rebuild_package

if __name__ == "__main__":
    value = rebuild_package()
    print(json.dumps({key: value[key] for key in ("plugin_id", "package_hash", "release_id", "wheel_path")}, sort_keys=True))
