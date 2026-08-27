"""Canonical operation-ledger context identity from formal design §20.5.

The wire meta remains unchanged.  Fencing epochs are checked before deriving
the identity, but they are deliberately excluded from the stable projection so
an ACK-loss retry cannot create a second durable idempotency row.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .canonical import canonical_bytes, sha256_hex
from .errors import ContractError, ContractValidationError, ErrorCode


_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_UTC = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)
_COMMON_META = {
    "protocol_version",
    "generation_id",
    "plugin_release_id",
    "deadline_at",
    "context",
    "operation_id",
}
_MISSING_EPOCH = object()


def _require_exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractValidationError(f"{label} fields are not closed: missing={missing}, extra={extra}")


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not a v1 ID")
    return value


def operation_context_projection(
    meta: Mapping[str, Any],
    *,
    expected_lease_epoch: int | None | object = _MISSING_EPOCH,
) -> dict[str, Any]:
    """Return the closed identity projection after profile/fencing checks."""

    if not isinstance(meta, Mapping):
        raise ContractValidationError("RPC meta must be an object")
    context = meta.get("context")
    profile_fields: set[str]
    if context == "control":
        profile_fields = set()
    elif context == "install":
        profile_fields = {"install_operation_id", "install_lease_epoch"}
    elif context == "attempt":
        profile_fields = {"job_id", "step_id", "attempt_id", "lease_epoch"}
    else:
        raise ContractValidationError("unknown RPC context profile")
    _require_exact(meta, _COMMON_META | profile_fields, f"{context} RPC meta")
    if meta.get("protocol_version") != "1":
        raise ContractValidationError("RPC protocol_version must be 1")
    generation_id = _require_id(meta.get("generation_id"), "generation_id")
    plugin_release_id = meta.get("plugin_release_id")
    if not isinstance(plugin_release_id, str) or _HASH.fullmatch(plugin_release_id) is None:
        raise ContractValidationError("plugin_release_id must be lowercase SHA-256")
    _require_id(meta.get("operation_id"), "operation_id")
    if not isinstance(meta.get("deadline_at"), str) or _UTC.fullmatch(meta["deadline_at"]) is None:
        raise ContractValidationError("deadline_at must use the frozen UTC format")

    projection: dict[str, Any] = {
        "schema": "operation-context-identity/v1",
        "protocol_version": "1",
        "context": context,
        "generation_id": generation_id,
        "plugin_release_id": plugin_release_id,
    }
    if context == "control":
        if expected_lease_epoch is not _MISSING_EPOCH and expected_lease_epoch is not None:
            raise ContractValidationError("control context has no lease epoch to fence")
    if context == "install":
        projection["install_operation_id"] = _require_id(meta.get("install_operation_id"), "install_operation_id")
        epoch = meta.get("install_lease_epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ContractValidationError("install_lease_epoch must be a positive integer")
        if expected_lease_epoch is _MISSING_EPOCH or expected_lease_epoch is None:
            raise ContractValidationError("install context derivation requires the current lease epoch")
        if not isinstance(expected_lease_epoch, int) or isinstance(expected_lease_epoch, bool) or expected_lease_epoch < 1:
            raise ContractValidationError("expected lease epoch must be a positive integer")
        if epoch != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "install lease epoch is stale")
    elif context == "attempt":
        projection.update(
            {
                "job_id": _require_id(meta.get("job_id"), "job_id"),
                "step_id": _require_id(meta.get("step_id"), "step_id"),
                "attempt_id": _require_id(meta.get("attempt_id"), "attempt_id"),
            }
        )
        epoch = meta.get("lease_epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ContractValidationError("lease_epoch must be a positive integer")
        if expected_lease_epoch is _MISSING_EPOCH or expected_lease_epoch is None:
            raise ContractValidationError("attempt context derivation requires the current lease epoch")
        if not isinstance(expected_lease_epoch, int) or isinstance(expected_lease_epoch, bool) or expected_lease_epoch < 1:
            raise ContractValidationError("expected lease epoch must be a positive integer")
        if epoch != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "attempt lease epoch is stale")
    return projection


def derive_operation_context_identity(
    meta: Mapping[str, Any],
    *,
    expected_lease_epoch: int | None | object = _MISSING_EPOCH,
) -> str:
    """Return SHA-256(domain separator || JCS(closed stable projection))."""

    projection = operation_context_projection(meta, expected_lease_epoch=expected_lease_epoch)
    return sha256_hex(b"plotpilot-operation-context/v1\n" + canonical_bytes(projection))


__all__ = ["derive_operation_context_identity", "operation_context_projection"]
