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

export interface SourceRef {
  workspace_id: Id | null
  source_type: string
  source_id: Id
  revision_or_hash: string
}

export interface CandidateItem {
  schema: 'candidate-item/v1'
  item_id: Id
  item_kind: 'document' | 'node_structure' | 'relation_set' | 'incomplete_stream'
  status: 'complete' | 'partial' | 'failed' | 'skipped'
  target: Target
  base: { revision_id: Id; content_hash: Hash }
  write_set: WriteSetEntry[]
  mutation: { mode: 'replace' | 'text_patch' | 'structure_patch' | 'relation_patch' | 'append_text'; payload_schema: string; payload_hash: Hash }
  payload_asset_id: Id
  parent_candidate_ids: Id[]
  source_refs: SourceRef[]
}

export type CandidateStageState = 'staged' | 'published' | 'rolled_back'

export interface CandidateStageReceipt {
  candidate_ids: Id[]
  state: CandidateStageState
}

export interface ArtifactItem {
  schema: 'artifact-item/v1'
  item_id: Id
  artifact_kind: string
  payload_asset_id: Id
  payload_hash: Hash
  mime: string
  source_refs: SourceRef[]
  status: 'complete' | 'partial' | 'failed' | 'skipped'
}

export interface DiagnosticItem {
  schema: 'diagnostic-item/v1'
  item_id: Id
  severity: 'info' | 'warning' | 'error'
  code: string
  message: string
  details_asset_id: Id | null
  details_hash: Hash | null
  source_refs: SourceRef[]
  status: 'complete' | 'failed' | 'skipped'
}

export interface SkillChainRef {
  schema: 'skill-chain-ref/v1'
  chain_result_id: Id
  result_bundle_id: Id | null
  result_item_id: Id | null
  stream_id: Id | null
  acked_prefix_hash: Hash | null
  asset_id: Id | null
  asset_hash: Hash | null
}

export interface Checkpoint {
  schema: 'checkpoint/v1'
  checkpoint_id: Id
  checkpoint_seq: number
  job_id: Id
  step_id: Id
  source_attempt_id: Id
  lease_epoch: number
  run_snapshot_hash: Hash
  replay_policy: 'idempotent_auto' | 'checkpoint_resume' | 'manual_if_unknown' | 'never_replay'
  completed_units: number
  total_units: number | null
  unit_set_hash: Hash | null
  state_asset_id: Id | null
  created_at: string
  checkpoint_hash: Hash
}

export interface StreamPrefix {
  schema: 'stream-prefix/v1'
  stream_id: Id
  job_id: Id
  step_id: Id
  output_role: string
  target: Target
  attempt_id: Id
  lease_epoch: number
  prefix_seq: number
  prefix_asset_id: Id
  prefix_hash: Hash
  byte_length: number
  encoding: 'utf-8'
}

export interface SkillPatch {
  patch_id: Id
  start_codepoint: number
  end_codepoint: number
  replacement_asset_id: Id
  replacement_hash: Hash
  before_hash: Hash
  after_hash: Hash
  verified: boolean
}

export interface SkillRunReceipt {
  schema: 'skill-run-receipt/v1'
  receipt_id: Id
  chain_id: Id
  chain_index: number
  run_snapshot_hash: Hash
  result_bundle_id: Id | null
  result_item_id: Id | null
  stream_id: Id | null
  acked_prefix_hash: Hash | null
  skill_id: Id
  release_id: Hash
  package_hash: Hash
  parameters_asset_id: Id | null
  input_asset_id: Id
  input_hash: Hash
  output_asset_id: Id | null
  output_hash: Hash | null
  step_state: 'executed' | 'failed' | 'skipped'
  frozen: boolean
  participated: boolean
  model_claimed: boolean
  verified_patch: boolean
  claim_evidence_asset_id: Id | null
  patches: SkillPatch[]
  warnings: Array<{ code: string; message: string; details_asset_id: Id | null }>
  previous_receipt_hash: Hash | null
  receipt_hash: Hash
}

export interface SkillChainResult {
  schema: 'skill-chain-result/v1'
  chain_id: Id
  run_snapshot_hash: Hash
  result_bundle_id: Id | null
  result_item_id: Id | null
  stream_id: Id | null
  acked_prefix_hash: Hash | null
  receipt_ids: Id[]
  receipt_hashes: Hash[]
  chain_status: 'succeeded' | 'partial' | 'failed' | 'cancelled'
  input_hash: Hash
  final_output_asset_id: Id | null
  final_output_hash: Hash | null
  chain_hash: Hash
}

export interface ProvenanceReceipt {
  schema: 'provenance-receipt/v1'
  receipt_id: Id
  plugin_id: Id
  release_id: Hash
  package_hash: Hash
  capability_id: Id
  job_id: Id
  step_id: Id
  attempt_id: Id
  lease_epoch: number
  run_snapshot_hash: Hash
  bundle_id: Id | null
  bundle_hash: Hash | null
  parent_receipt_ids: Id[]
  model_receipt_ids: Id[]
  skill_chain_result_refs: SkillChainRef[]
  staged_items: Id[]
  created_at: string
  receipt_hash: Hash
}

export interface SseRecovery {
  schema: 'sse-recovery/v1'
  stream_kind: 'core_event' | 'job_event'
  aggregate_id: Id | null
  requested_after_seq: number
  replay_floor_seq: number
  durable_high_water_seq: number
  gap: boolean
  snapshot_required: boolean
  snapshot_schema: string | null
  snapshot_revision: number | null
  snapshot_asset_id: Id | null
  snapshot_hash: Hash | null
}

export interface SettingsValidationReceipt {
  schema: 'settings-validation-receipt/v1'
  receipt_id: Id
  plugin_id: Id
  plugin_release_id: Hash
  settings_revision_id: Id
  schema_hash: Hash
  payload_hash: Hash
  valid: boolean
  details_asset_id: Id | null
  created_at: string
  receipt_hash: Hash
}

export interface PluginLifecycleTransition {
  schema: 'plugin-lifecycle-transition/v1'
  install_operation_id: Id
  base_generation_id: Id | null
  base_lkg_generation_id: Id | null
  target_generation_id: Id | null
  state: 'selected' | 'staged' | 'package_published' | 'env_prepared' | 'shadow_prepared' | 'migrated' | 'settings_validated' | 'qualified' | 'pending_apply' | 'current_committed' | 'lkg_pending' | 'lkg_promoted' | 'failed' | 'superseded' | 'rollback_armed' | 'rolled_back' | 'safe_mode'
  package_store_status: 'absent' | 'staged' | 'published' | 'orphan'
  shadow_data_generation_id: Id | null
  target_settings_revision_ids: Array<{ plugin_id: Id; settings_revision_id: Id }>
  qualification_id: Id | null
  rollback_attempt: 0 | 1
  rollback_token: Id | null
  failure_code: string | null
  created_at: string
  updated_at: string
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
