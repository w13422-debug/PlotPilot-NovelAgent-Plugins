"""Locate the contract workspace without assuming a fixed SDK depth."""
from __future__ import annotations

from pathlib import Path


def find_workspace_root(start: Path | None = None) -> Path:
    """Return the nearest ancestor containing the frozen contract manifest."""

    location = (start or Path(__file__)).resolve()
    if location.is_file():
        location = location.parent
    for candidate in (location, *location.parents):
        if (candidate / "contracts" / "manifest-v1.json").is_file():
            return candidate
    raise RuntimeError(
        "PlotPilot contract workspace not found: expected contracts/manifest-v1.json "
        f"above {location}"
    )


WORKSPACE_ROOT = find_workspace_root()

__all__ = ["WORKSPACE_ROOT", "find_workspace_root"]
