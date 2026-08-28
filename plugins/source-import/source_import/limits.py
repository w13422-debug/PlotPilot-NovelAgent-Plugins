"""Deterministic resource profile for the source importer.

The limits are code-owned constants, not environment-controlled knobs.  A
Core-owned Asset is read in bounded pages before any format parser runs.
"""
from __future__ import annotations

from dataclasses import dataclass


class ImportLimitError(ValueError):
    def __init__(self, resource: str, actual: int | float, limit: int | float) -> None:
        self.resource = resource
        self.actual = actual
        self.limit = limit
        super().__init__(f"{resource} exceeds limit: {actual} > {limit}")


@dataclass(frozen=True)
class ImportLimits:
    max_original_bytes: int = 256 * 1024 * 1024
    max_page_bytes: int = 1 * 1024 * 1024
    max_pages: int = 4096
    max_text_characters: int = 10_000_000
    max_xml_bytes: int = 16 * 1024 * 1024
    max_xml_depth: int = 128
    max_zip_entries: int = 10_000
    max_zip_member_bytes: int = 64 * 1024 * 1024
    max_zip_total_bytes: int = 512 * 1024 * 1024
    max_zip_compression_ratio: float = 200.0
    max_spine_items: int = 4096
    max_nav_entries: int = 10_000
    max_structure_nodes: int = 100_000
    max_source_name_scalars: int = 240

    def validate(self) -> None:
        for name, value in self.__dict__.items():
            if name == "max_zip_compression_ratio":
                if not isinstance(value, (int, float)) or value <= 0:
                    raise ValueError(f"{name} must be positive")
            elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_LIMITS = ImportLimits()


def ensure_size(resource: str, actual: int, limit: int) -> None:
    if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
        raise ValueError(f"{resource} size is invalid")
    if actual > limit:
        raise ImportLimitError(resource, actual, limit)


def ensure_count(resource: str, actual: int, limit: int) -> None:
    if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
        raise ValueError(f"{resource} count is invalid")
    if actual > limit:
        raise ImportLimitError(resource, actual, limit)
