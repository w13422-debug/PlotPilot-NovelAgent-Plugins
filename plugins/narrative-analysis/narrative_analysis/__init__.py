"""PlotPilot NAP-03 narrative-analysis Code Plugin."""
from .contract import (
    EvidenceSpanError, NarrativeContractError, build_evidence_span,
    compile_narrative_plan, hash_json, sha256_text, validate_evidence_span,
    validate_narrative_plan, validate_narrative_synthesis,
    validate_narrative_unit, validate_nodes,
)

__all__ = [
    "CAPABILITIES", "DESCRIPTORS", "EvidenceSpanError", "HostPort", "NEEDS",
    "NarrativeAnalysisPlugin", "NarrativeAnalysisWorkerError",
    "NarrativeContractError", "PLUGIN_ID", "RELEASE_ID", "VERSION",
    "build_evidence_span", "capability_descriptor", "compile_narrative_plan",
    "hash_json", "main", "sha256_text", "validate_evidence_span",
    "validate_narrative_plan", "validate_narrative_synthesis",
    "validate_narrative_unit", "validate_nodes",
]

_RUNTIME_EXPORTS = frozenset({
    "CAPABILITIES", "DESCRIPTORS", "HostPort", "NEEDS", "PLUGIN_ID",
    "RELEASE_ID", "VERSION", "NarrativeAnalysisPlugin",
    "NarrativeAnalysisWorkerError", "capability_descriptor", "main",
})

def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        from . import runtime
        return getattr(runtime, name)
    raise AttributeError(name)
