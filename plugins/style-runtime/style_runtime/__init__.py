"""PlotPilot NAP-05 exact-qualified Style Runtime Code Plugin."""
from .contract import (
    StyleContractError,
    calculate_style_payload_hash,
    calculate_style_release_id,
    merge_lexicons,
    validate_qualification_receipt,
    validate_qualified_style,
    validate_style_pack,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_APPLY",
    "CAPABILITY_REFINE",
    "CAPABILITY_REVIEW",
    "DESCRIPTORS",
    "NEEDS",
    "PACKAGE_HASH",
    "PLUGIN_ID",
    "RELEASE_ID",
    "VERSION",
    "HostPort",
    "StyleContractError",
    "StyleRuntimePlugin",
    "StyleRuntimeWorkerError",
    "calculate_style_payload_hash",
    "calculate_style_release_id",
    "capability_descriptor",
    "main",
    "merge_lexicons",
    "validate_qualification_receipt",
    "validate_qualified_style",
    "validate_style_pack",
]

_RUNTIME_EXPORTS = frozenset({
    "CAPABILITIES", "CAPABILITY_APPLY", "CAPABILITY_REVIEW", "CAPABILITY_REFINE",
    "DESCRIPTORS", "HostPort", "NEEDS", "PACKAGE_HASH", "PLUGIN_ID", "RELEASE_ID",
    "StyleRuntimePlugin", "StyleRuntimeWorkerError", "VERSION", "capability_descriptor", "main",
})


def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        from . import runtime
        return getattr(runtime, name)
    raise AttributeError(name)
