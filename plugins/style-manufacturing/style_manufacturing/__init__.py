"""PlotPilot NAP-05 Style Manufacturing Code Plugin."""
from .contract import (StyleContractError, build_qualification_receipt, canonical_json,
    exact_release_eligible, hash_json, merge_lexicons, sha256_bytes, sha256_text,
    style_payload_hash, style_release_id, validate_lexicon, validate_qualification_receipt,
    validate_style_pack)

_RUNTIME_EXPORTS = frozenset({"CAPABILITIES","CAPABILITY_MANUFACTURE","CAPABILITY_PACKAGE","CAPABILITY_QUALIFY",
    "DESCRIPTORS","HostPort","NEEDS","PACKAGE_HASH","PLUGIN_ID","RELEASE_ID","StyleManufacturingPlugin",
    "StyleManufacturingWorkerError","TerminalContractError","VERSION","capability_descriptor","main"})

def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        from . import runtime
        return getattr(runtime,name)
    raise AttributeError(name)

__all__ = sorted(set(_RUNTIME_EXPORTS) | {"StyleContractError","build_qualification_receipt","canonical_json",
    "exact_release_eligible","hash_json","merge_lexicons","sha256_bytes","sha256_text","style_payload_hash",
    "style_release_id","validate_lexicon","validate_qualification_receipt","validate_style_pack"})
