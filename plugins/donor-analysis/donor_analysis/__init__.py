"""PlotPilot NAP-02 donor-analysis Code Plugin."""
from .contract import (
    ATOM_KINDS, DonorContractError, EvidenceSpanError, bind_current_accepted_atoms,
    build_evidence_span, hash_json, sha256_text, validate_atom_payload,
    validate_claim_authority, validate_claim_input, validate_claim_payload, validate_evidence_span,
    validate_nodes, validate_rereview_records,
)
__all__ = ["ATOM_KINDS", "CAPABILITIES", "DESCRIPTORS", "DonorAnalysisPlugin",
           "DonorAnalysisWorkerError", "DonorContractError", "EvidenceSpanError", "HostPort",
           "NEEDS", "PLUGIN_ID", "RELEASE_ID", "VERSION", "bind_current_accepted_atoms",
           "build_evidence_span", "capability_descriptor", "hash_json", "main", "sha256_text",
           "validate_atom_payload", "validate_claim_authority", "validate_claim_input", "validate_claim_payload",
           "validate_evidence_span", "validate_nodes", "validate_rereview_records"]

_RUNTIME_EXPORTS = frozenset({"CAPABILITIES", "DESCRIPTORS", "NEEDS", "PLUGIN_ID", "RELEASE_ID", "VERSION",
    "DonorAnalysisPlugin", "DonorAnalysisWorkerError", "HostPort", "capability_descriptor", "main"})

def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        from . import runtime
        return getattr(runtime, name)
    raise AttributeError(name)
