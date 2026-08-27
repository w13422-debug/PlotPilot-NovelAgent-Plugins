"""Stable SDK errors and application error codes from formal design §20.5."""
from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    INCOMPATIBLE_GENERATION = 1001
    STALE_LEASE = 1002
    CANCELLED = 1003
    DEADLINE_EXCEEDED = 1004
    ASSET_ERROR = 1005
    SETTINGS_INVALID = 1006
    MIGRATION_FAILED = 1007
    DUPLICATE_REQUEST = 1008
    UNCERTAIN_EXTERNAL_EFFECT = 1009
    INVALID_TRANSITION = 1010
    RESULT_CONTRACT_MISMATCH = 1011
    DATA_INTERPRETER_UNAVAILABLE = 1012
    RELEASE_RETIRING = 1013
    CHECKPOINT_INVALID = 1014


class ContractError(ValueError):
    """An expected, machine-readable contract or protocol failure."""

    def __init__(self, code: int | ErrorCode, message: str, *, path: str | None = None, details: Any = None) -> None:
        self.code = int(code)
        self.path = path
        self.details = details
        suffix = f" at {path}" if path else ""
        super().__init__(f"{self.code}: {message}{suffix}")


class ContractValidationError(ContractError):
    """Schema/semantic validation failed before a durable mutation."""

    def __init__(self, message: str, *, path: str | None = None, details: Any = None) -> None:
        super().__init__(ErrorCode.RESULT_CONTRACT_MISMATCH, message, path=path, details=details)
