"""TypedDict surface consumed by future P1-P6 projects.

The runtime verifier remains authoritative; these types are intentionally
data-only and do not expose a database connection or a publication method.
"""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


Hash = str
ContractId = Literal["candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"]


class Scope(TypedDict):
    document_id: str | None
    node_id: str | None
    operation: str


class InputRevision(TypedDict):
    document_id: str
    revision_id: str
    content_hash: Hash


class AssetHash(TypedDict):
    asset_id: str
    sha256: Hash


class EvidenceSpan(TypedDict):
    schema: Literal["evidence-span/v1"]
    workspace_id: str
    document_id: str
    revision_id: str
    node_id: str
    start_codepoint: int
    end_codepoint: int
    quote: str
    quote_hash: Hash
    canonical_text_hash: Hash


class ClaimInputAtom(TypedDict):
    ordinal: int
    atom_id: str
    payload_hash: Hash
    acceptance_ordinal: int
    evidence_spans: list[EvidenceSpan]


class ClaimInput(TypedDict):
    schema: Literal["claim-input/v1"]
    source_revision_id: str
    ordered_atoms: list[ClaimInputAtom]


class RunSnapshot(TypedDict):
    schema: Literal["run-snapshot/v1"]
    snapshot_id: str
    core_contract_version: str
    workspace_id: str
    scope: Scope
    input_revisions: list[InputRevision]
    plan_revision_id: str
    plugin_releases: list[dict[str, object]]
    plugin_settings_revisions: list[dict[str, object]]
    data_bindings: list[dict[str, object]]
    skill_releases: list[dict[str, object]]
    model_profile_revision_id: str | None
    parameters_asset_id: str | None
    asset_hashes: list[AssetHash]
    request_key: Hash
    run_intent_id: str
    created_at: str
    snapshot_hash: Hash


class ResultBundle(TypedDict):
    schema: Literal["result-bundle/v1"]
    contract_id: ContractId
    bundle_id: str
    bundle_type: Literal["candidate_batch", "artifact", "diagnostic"]
    producer: dict[str, object]
    input_snapshot_hash: Hash
    items: list[dict[str, object]]
    warnings: list[dict[str, object]]
    partial: bool
    provenance_receipt_id: str
    skill_chain_result_refs: list[dict[str, object]]


class PluginUIIntent(TypedDict):
    schema: Literal["plugin-ui-intent/v1"]
    intent_id: str
    intent_seq: int
    render_seq: int
    action_id: str
    event_type: str
    intent_kind: Literal["invoke_capability", "open_core_operation", "request_candidate_preview", "request_job_cancel", "set_view_state"]
    capability_id: str | None
    payload_asset_id: str | None
    operation_key: str
    freshness: dict[str, object]


class SkillRunReceipt(TypedDict):
    schema: Literal["skill-run-receipt/v1"]
    receipt_id: str
    chain_id: str
    chain_index: int
    run_snapshot_hash: Hash
    result_bundle_id: str | None
    result_item_id: str | None
    stream_id: str | None
    acked_prefix_hash: Hash | None
    skill_id: str
    release_id: Hash
    package_hash: Hash
    parameters_asset_id: str | None
    input_asset_id: str
    input_hash: Hash
    output_asset_id: str | None
    output_hash: Hash | None
    step_state: Literal["executed", "failed", "skipped"]
    frozen: bool
    participated: bool
    model_claimed: bool
    verified_patch: bool
    claim_evidence_asset_id: str | None
    patches: list[dict[str, object]]
    warnings: list[dict[str, object]]
    previous_receipt_hash: Hash | None
    receipt_hash: Hash


class BackupBundle(TypedDict):
    schema: Literal["backup-bundle/v1"]
    backup_id: str
    library_root_id: str
    backup_epoch: int
    mode: Literal["full", "data", "workspace"]
    workspace_ids: list[str]
    core_contract_version: str
    core_snapshot_hash: Hash
    workspace_snapshot_hash: Hash | None
    current_generation_id: str | None
    lkg_generation_id: str | None
    asset_closure_root: Hash
    plugin_releases: list[dict[str, object]]
    projection_rebuild_required: list[dict[str, object]]
    files: list[dict[str, object]]
    created_at: str
    verification: dict[str, object]
    bundle_hash: Hash
