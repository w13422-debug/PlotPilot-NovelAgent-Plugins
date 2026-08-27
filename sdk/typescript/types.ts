/**
 * Lightweight TypeScript DTOs for the frozen v1 contract surface.
 *
 * JSON Schema files under ../../contracts remain the only shape authority;
 * these declarations are ergonomic compile-time views and intentionally do
 * not duplicate schema definitions or expose Core persistence.
 */

export type Hash = string
export type Id = string
export type ContractId = 'candidate-batch/v1' | 'artifact-bundle/v1' | 'diagnostic-bundle/v1'

export interface Scope {
  document_id: Id | null
  node_id: Id | null
  operation: Id
}

export interface InputRevision {
  document_id: Id
  revision_id: Id
  content_hash: Hash
}

export interface AssetHash {
  asset_id: Id
  sha256: Hash
}

export interface PluginReleaseBinding {
  plugin_id: Id
  release_id: Id
  package_hash: Hash
  data_generation_id: Id | null
}

export interface SettingsBinding {
  plugin_id: Id
  scope: string
  scope_id: Id | null
  settings_revision_id: Id
  schema_hash: Hash
  validated_by_release_id: Id
}

export interface DataBinding {
  data_plugin_id: Id
  data_release_id: Id
  format_id: Id
  interpreter_binding_id: Id
  order: number
  bundle_asset_id: Id
  bundle_hash: Hash
}

export interface PlanBinding {
  binding_id: Id
  capability_id: Id
  plugin_id: Id
  release_requirement: string
  order: number
  enabled: boolean
  required: boolean
  propagate_cancel: boolean
  parameters_asset_id: Id | null
}

export interface PlanDataBinding {
  data_binding_id: Id
  data_plugin_id: Id
  release_requirement: string
  format_id: Id
  interpreter_binding_id: Id
  order: number
  enabled: boolean
  parameters_asset_id: Id | null
}

export interface PluginPlan {
  schema: 'plugin-plan/v1'
  plan_id: Id
  revision: number
  name: string
  description: string
  bindings: PlanBinding[]
  data_bindings: PlanDataBinding[]
  skill_preset_revision_id: Id | null
  result_mode: 'separate' | 'synthesize'
  synthesizer: { binding_id: Id; capability_id: Id; plugin_id: Id; release_requirement: string } | null
  model_profile_revision_id: Id | null
  ui_defaults: Array<{ slot: Id; expanded: boolean }>
}

export interface SkillBinding {
  skill_id: Id
  release_id: Id
  package_hash: Hash
  parameters_asset_id: Id | null
  order: number
}

export interface RunSnapshot {
  schema: 'run-snapshot/v1'
  snapshot_id: Id
  core_contract_version: string
  workspace_id: Id
  scope: Scope
  input_revisions: InputRevision[]
  plan_revision_id: Id
  plugin_releases: PluginReleaseBinding[]
  plugin_settings_revisions: SettingsBinding[]
  data_bindings: DataBinding[]
  skill_releases: SkillBinding[]
  model_profile_revision_id: Id | null
  parameters_asset_id: Id | null
  asset_hashes: AssetHash[]
  request_key: Hash
  run_intent_id: Id
  created_at: string
  snapshot_hash: Hash
}

export interface EvidenceSpan {
  schema: 'evidence-span/v1'
  workspace_id: Id
  document_id: Id
  revision_id: Id
  node_id: Id
  start_codepoint: number
  end_codepoint: number
  quote: string
  quote_hash: Hash
  canonical_text_hash: Hash
}

export interface ClaimInputAtom {
  ordinal: number
  atom_id: Id
  payload_hash: Hash
  acceptance_ordinal: number
  evidence_spans: EvidenceSpan[]
}

export interface ClaimInput {
  schema: 'claim-input/v1'
  source_revision_id: Id
  ordered_atoms: ClaimInputAtom[]
}

export interface Target {
  workspace_id: Id
  entity_kind: 'document' | 'node_structure' | 'relation_set'
  entity_id: Id
}

export interface WriteSetEntry extends Target {
  revision_id: Id
  content_hash: Hash
}

export interface CandidateItem {
  schema: 'candidate-item/v1'
  item_id: Id
  item_kind: 'document' | 'node_structure' | 'relation_set' | 'incomplete_stream'
  status: 'complete' | 'partial' | 'failed' | 'skipped'
  target: Target
  base: { revision_id: Id; content_hash: Hash }
  write_set: WriteSetEntry[]
  mutation: { mode: string; payload_schema: string; payload_hash: Hash }
  payload_asset_id: Id
  parent_candidate_ids: Id[]
  source_refs: Record<string, unknown>[]
}

export interface ArtifactItem {
  schema: 'artifact-item/v1'
  item_id: Id
  item_kind: string
  status: 'complete' | 'partial' | 'failed' | 'skipped'
  asset_id: Id
  asset_hash: Hash
  artifact_schema: string
  source_refs: Record<string, unknown>[]
}

export interface DiagnosticItem {
  schema: 'diagnostic-item/v1'
  item_id: Id
  item_kind: string
  status: 'complete' | 'partial' | 'failed' | 'skipped'
  severity: 'info' | 'warning' | 'error'
  code: string
  message: string
  details_asset_id: Id | null
  source_refs: Record<string, unknown>[]
}

export interface SkillChainRef {
  schema: 'skill-chain-ref/v1'
  result_bundle_id: Id | null
  result_item_id: Id | null
  stream_id: Id | null
  acked_prefix_hash: Hash | null
  asset_id: Id | null
  asset_hash: Hash | null
}

export interface ResultBundle {
  schema: 'result-bundle/v1'
  contract_id: ContractId
  bundle_id: Id
  bundle_type: 'candidate_batch' | 'artifact' | 'diagnostic'
  producer: Record<string, unknown>
  input_snapshot_hash: Hash
  items: Array<CandidateItem | ArtifactItem | DiagnosticItem>
  warnings: Array<{ code: string; message: string; details_asset_id: Id | null }>
  partial: boolean
  provenance_receipt_id: Id
  skill_chain_result_refs: SkillChainRef[]
}

export interface RpcMetaBase {
  protocol_version: '1'
  generation_id: Id
  plugin_release_id: Hash
  deadline_at: string
  operation_id: Id
}

export interface RpcControlMeta extends RpcMetaBase { context: 'control' }
export interface RpcInstallMeta extends RpcMetaBase { context: 'install'; install_operation_id: Id; install_lease_epoch: number }
export interface RpcAttemptMeta extends RpcMetaBase { context: 'attempt'; job_id: Id; step_id: Id; attempt_id: Id; lease_epoch: number }
export type RpcMeta = RpcControlMeta | RpcInstallMeta | RpcAttemptMeta

export type RpcMethod =
  | 'runtime.handshake' | 'runtime.health' | 'runtime.heartbeat' | 'capability.describe' | 'settings.validate'
  | 'migration.plan' | 'migration.apply' | 'migration.verify' | 'job.start' | 'job.resume' | 'job.pause'
  | 'job.cancel' | 'runtime.shutdown'
  | 'host.asset.read/v1' | 'host.asset.create/v1' | 'host.asset.upload.status/v1' | 'host.model.invoke/v1'
  | 'host.capability.invoke/v1' | 'host.capability.poll/v1' | 'host.capability.cancel/v1' | 'host.candidate.stage/v1'
  | 'host.checkpoint.commit/v1' | 'host.stream.commit/v1' | 'host.job.event/v1' | 'host.job.await_user/v1'
  | 'host.job.complete/v1' | 'host.log/v1' | 'host.migration.lease.renew/v1' | 'host.migration.lease.release/v1'

export interface RpcRequest {
  jsonrpc: '2.0'
  id: Id
  method: RpcMethod
  meta: RpcMeta
  params: Record<string, unknown>
}

export interface RpcNotification {
  jsonrpc: '2.0'
  method: 'runtime.heartbeat'
  meta: RpcAttemptMeta
  params: Record<string, unknown>
}

export interface RpcSuccess {
  jsonrpc: '2.0'
  id: Id
  result: Record<string, unknown>
}

export interface RpcError {
  jsonrpc: '2.0'
  id: Id | null
  error: { code: number; message: string; data: { error_id: Id; retryable: boolean; details_asset_id: Id | null } | null }
}

export interface PluginUIIntent {
  schema: 'plugin-ui-intent/v1'
  intent_id: Id
  intent_seq: number
  render_seq: number
  action_id: Id
  event_type: string
  intent_kind: 'invoke_capability' | 'open_core_operation' | 'request_candidate_preview' | 'request_job_cancel' | 'set_view_state'
  capability_id: Id | null
  payload_asset_id: Id | null
  operation_key: Id
  freshness: { generation_id: Id; plugin_release_id: Hash; workspace_id: Id | null; workspace_revision_id: Id | null; plan_revision_id: Id | null }
}

export interface BackupBundle {
  schema: 'backup-bundle/v1'
  backup_id: Id
  library_root_id: Id
  backup_epoch: number
  mode: 'full' | 'data' | 'workspace'
  workspace_ids: Id[]
  core_contract_version: string
  core_snapshot_hash: Hash
  workspace_snapshot_hash: Hash | null
  current_generation_id: Id | null
  lkg_generation_id: Id | null
  asset_closure_root: Hash
  plugin_releases: Array<{ plugin_id: Id; release_id: Hash; package_hash: Hash; package_present: boolean }>
  projection_rebuild_required: Array<{ plugin_id: Id; release_id: Hash; reason: 'plugin_projection_rebuild_required' }>
  files: Array<{ path: string; size: number; sha256: Hash; role: string }>
  created_at: string
  verification: { databases_valid: boolean; assets_valid: boolean; files_valid: boolean; compatible: boolean; verified_at: string }
  bundle_hash: Hash
}

export type JsonObject = Record<string, unknown>
