import { canonicalJson, hashJcs, sha256Hex, utf8 } from './canonical.ts'
import unicodeCasefoldContractJson from '../../contracts/unicode-casefold-v1.json' with { type: 'json' }
import rpcMethodMatrixJson from '../../contracts/json-schema/rpc-method-matrix.v1.json' with { type: 'json' }
import { assertValidContract, frozenSchemaInventory, schemaErrors } from './schema-validator.ts'
import type {
  BackupBundle,
  AuthoritativeAtom,
  CandidateItem,
  CandidateStageReceipt,
  Checkpoint,
  EvidenceSpan,
  JsonObject,
  PluginLifecycleTransition,
  ProvenanceReceipt,
  ResultBundle,
  RunSnapshot,
  SkillChainResult,
  SkillRunReceipt,
  SseRecovery,
  StreamPrefix,
} from './types.ts'

type ByteFiles = Record<string, Uint8Array>
type CasefoldContract = {
  schema: string
  unicode_data_version: string
  algorithm: string
  mappings: Record<string, string>
  nfc_decomposition: Record<string, number[]>
  nfc_combining_class: Record<string, number>
  nfc_composition: Record<string, number>
  nfc_hangul: Record<string, number>
}
type RpcDefinition = { meta_profile: string; params: { fields: string[] }; result: { fields: string[] } }

export type AuthoritativeAtomSource =
  | Record<string, AuthoritativeAtom>
  | readonly AuthoritativeAtom[]
  | ((atomId: string) => AuthoritativeAtom | null | undefined)

export interface ClaimInputStructureOptions {
  expectedSourceRevisionId?: string
  canonicalText?: string
  expectedCanonicalTextHash?: string
}

export interface ClaimInputValidationOptions extends ClaimInputStructureOptions {
  acceptedAtoms?: AuthoritativeAtomSource | null
}

export interface ClaimInputAuthorityOptions extends ClaimInputStructureOptions {
  acceptedAtoms: AuthoritativeAtomSource
}

export interface ResultVerificationContext {
  snapshotWorkspaceId?: string | null
  snapshotHashValue?: string | null
  knownParentIds?: Iterable<string>
}

const unicodeCasefoldContract = unicodeCasefoldContractJson as unknown as CasefoldContract
const rpcMethodMatrix = rpcMethodMatrixJson as unknown as {
  methods: Record<string, RpcDefinition>
  error_codes: Record<string, string>
}
const HASH = /^[0-9a-f]{64}$/
const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const UTC = /^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$/
const RESERVED = new Set([
  'con', 'prn', 'aux', 'nul', 'clock$',
  ...Array.from({ length: 9 }, (_, index) => `com${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `lpt${index + 1}`),
])
const HANGUL_PROFILE = { s_base: 0xAC00, l_base: 0x1100, v_base: 0x1161, t_base: 0x11A7, l_count: 19, v_count: 21, t_count: 28 }

if (
  unicodeCasefoldContract.schema !== 'unicode-casefold/v1' ||
  unicodeCasefoldContract.unicode_data_version !== '15.0.0' ||
  unicodeCasefoldContract.algorithm !== 'NFC followed by per-code-point full casefold mapping' ||
  JSON.stringify(unicodeCasefoldContract.nfc_hangul) !== JSON.stringify(HANGUL_PROFILE)
) throw new Error('invalid Unicode casefold contract version')

function fail(message: string): never { throw new Error(message) }

function objectOf(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return fail(`${label} must be an object`)
  return value as JsonObject
}

function arrayOf(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) return fail(`${label} must be an array`)
  return value
}

function assertExactKeys(value: JsonObject, fields: readonly string[], label: string): void {
  const expected = new Set(fields)
  const actual = Object.keys(value)
  if (actual.length !== expected.size || actual.some(field => !expected.has(field))) {
    const missing = fields.filter(field => !Object.prototype.hasOwnProperty.call(value, field))
    const extra = actual.filter(field => !expected.has(field))
    fail(`${label} fields are not closed: missing=${missing.join(',')}, extra=${extra.join(',')}`)
  }
}

function assertUnique(values: Iterable<unknown>, label: string): void {
  const list = [...values]
  if (new Set(list).size !== list.length) fail(label)
}

function assertSchema(value: JsonObject, schema: string): void {
  // EvidenceSpan and claim-input are the two frozen v1 contracts whose
  // schemas are intentionally expressed by their dedicated cross-object
  // validators rather than a standalone JSON-Schema artifact.
  if (schema === 'evidence-span/v1' || schema === 'claim-input/v1') {
    if (value.schema !== schema) fail(`${schema} schema mismatch`)
    return
  }
  assertValidContract(schema, value)
}

/** Validate any contract in the frozen JSON-Schema inventory. */
export { assertValidContract, frozenSchemaInventory, schemaErrors }

export function assertHash(value: unknown, label: string): asserts value is string {
  if (typeof value !== 'string' || !HASH.test(value)) fail(`${label} must be lowercase SHA-256`)
}

function assertId(value: unknown, label: string): asserts value is string {
  if (typeof value !== 'string' || !ID.test(value)) fail(`${label} must be a PlotPilot ID`)
}

function compareUtf8(left: string, right: string): number {
  const a = utf8(left)
  const b = utf8(right)
  const length = Math.min(a.byteLength, b.byteLength)
  for (let index = 0; index < length; index += 1) {
    const leftByte = a[index] ?? 0
    const rightByte = b[index] ?? 0
    if (leftByte !== rightByte) return leftByte - rightByte
  }
  return a.byteLength - b.byteLength
}

function codepointKey(codepoint: number): string { return codepoint.toString(16).padStart(4, '0') }

export function unicodeNfc(value: string): string {
  const decomposed: number[] = []
  const appendDecomposed = (codepoint: number): void => {
    const parts = unicodeCasefoldContract.nfc_decomposition[codepointKey(codepoint)]
    if (parts == null) { decomposed.push(codepoint); return }
    for (const part of parts) appendDecomposed(part)
  }
  for (const character of value) appendDecomposed(character.codePointAt(0) as number)

  const combiningClass = (codepoint: number): number => unicodeCasefoldContract.nfc_combining_class[codepointKey(codepoint)] ?? 0
  const ordered: number[] = []
  for (const codepoint of decomposed) {
    const ccc = combiningClass(codepoint)
    if (ccc === 0) { ordered.push(codepoint); continue }
    let index = ordered.length
    while (index > 0 && combiningClass(ordered[index - 1] as number) > ccc) index -= 1
    ordered.splice(index, 0, codepoint)
  }

  const composed: number[] = []
  let starterIndex = -1
  let lastClass = 0
  for (const codepoint of ordered) {
    const ccc = combiningClass(codepoint)
    let composite: number | undefined
    if (starterIndex >= 0 && (lastClass === 0 || lastClass < ccc)) {
      composite = unicodeCasefoldContract.nfc_composition[`${codepointKey(composed[starterIndex] as number)}+${codepointKey(codepoint)}`]
    }
    if (composite != null) composed[starterIndex] = composite
    else {
      if (ccc === 0) starterIndex = composed.length
      composed.push(codepoint)
      lastClass = ccc
    }
  }
  return composed.map(codepoint => String.fromCodePoint(codepoint)).join('')
}

export function unicodeNfcCasefold(value: string): string {
  if (typeof value !== 'string') return fail('casefold identity requires a string')
  return [...unicodeNfc(value)].map(character => unicodeCasefoldContract.mappings[codepointKey(character.codePointAt(0) as number)] ?? character).join('')
}

export function normalizeWindowsPath(path: string): string {
  if (typeof path !== 'string' || !path || path.includes('\\') || path.includes('\0') || path.startsWith('/') || /^[A-Za-z]:/.test(path)) return fail('invalid package path')
  const normalized = unicodeNfc(path)
  const parts = normalized.split('/')
  if (parts.some(part => !part || part === '.' || part === '..' || /[<>"|?*:\u0000-\u001f]/.test(part) || /[. ]$/.test(part))) return fail('invalid Windows path segment')
  if (parts.some(part => RESERVED.has(unicodeNfcCasefold(part.split('.', 1)[0] ?? '')))) return fail('reserved Windows path')
  if ([...normalized].length > 240) return fail('path exceeds 240 Unicode scalar values')
  return normalized
}

function normalizedFileMap(files: ByteFiles): Map<string, Uint8Array> {
  const normalized = new Map<string, Uint8Array>()
  const identities = new Set<string>()
  for (const [rawPath, content] of Object.entries(files)) {
    const path = normalizeWindowsPath(rawPath)
    if (path === 'files.sha256') fail('files.sha256 is generated')
    if (!(content instanceof Uint8Array)) fail('package content must be raw bytes')
    const identity = unicodeNfcCasefold(path)
    if (identities.has(identity)) fail('casefold path collision')
    identities.add(identity)
    normalized.set(path, content)
  }
  return normalized
}

export async function buildFilesSha256(files: ByteFiles): Promise<Uint8Array> {
  const normalized = normalizedFileMap(files)
  const rows: string[] = []
  for (const path of [...normalized.keys()].sort(compareUtf8)) rows.push(`${await sha256Hex(normalized.get(path) as Uint8Array)}  ${path}\n`)
  return utf8(rows.join(''))
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0))
  let offset = 0
  for (const part of parts) { result.set(part, offset); offset += part.byteLength }
  return result
}

export async function packageDigest(files: ByteFiles, pluginId: string, version: string): Promise<{ filesSha256: Uint8Array; packageHash: string; releaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const packageHash = await sha256Hex(concatBytes(utf8('plotpilot-package/v1\n'), filesSha256))
  const releaseId = await sha256Hex(utf8(`plotpilot-release/v1\n${pluginId}\n${version}\n${packageHash}\n`))
  return { filesSha256, packageHash, releaseId }
}

export async function skillPackageDigest(files: ByteFiles, skillId: string, version: string): Promise<{ filesSha256: Uint8Array; skillPackageHash: string; skillReleaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const skillPackageHash = await sha256Hex(concatBytes(utf8('plotpilot-skill-package/v1\n'), filesSha256))
  const skillReleaseId = await sha256Hex(utf8(`plotpilot-skill-release/v1\n${skillId}\n${version}\n${skillPackageHash}\n`))
  return { filesSha256, skillPackageHash, skillReleaseId }
}

function normalizedSnapshot(snapshot: JsonObject): JsonObject {
  const result = structuredClone(snapshot) as JsonObject
  const sortBy = (field: string, key: (value: JsonObject) => string): void => {
    const values = result[field]
    if (Array.isArray(values)) result[field] = values.map(item => objectOf(item, field)).sort((a, b) => compareUtf8(key(a), key(b)))
  }
  sortBy('input_revisions', item => `${String(item.document_id ?? '')}\0${String(item.revision_id ?? '')}`)
  sortBy('plugin_releases', item => String(item.plugin_id ?? ''))
  sortBy('plugin_settings_revisions', item => `${String(item.plugin_id ?? '')}\0${String(item.scope ?? '')}\0${String(item.scope_id ?? '')}`)
  sortBy('asset_hashes', item => String(item.asset_id ?? ''))
  return result
}

export function requestKeyBytes(snapshot: RunSnapshot): Uint8Array {
  const parametersHash = snapshot.parameters_asset_id == null ? '-' : snapshot.asset_hashes.find(asset => asset.asset_id === snapshot.parameters_asset_id)?.sha256 ?? '-'
  const revisions = [...snapshot.input_revisions].sort((left, right) => compareUtf8(`${left.document_id}\0${left.revision_id}`, `${right.document_id}\0${right.revision_id}`))
  const revisionLine = revisions.map(item => `${item.document_id}=${item.revision_id}=${item.content_hash}`).join(',')
  return utf8([
    'request-key/v1', snapshot.workspace_id, snapshot.scope.operation,
    snapshot.scope.document_id ?? 'null', snapshot.scope.node_id ?? 'null',
    revisionLine, snapshot.plan_revision_id, parametersHash, snapshot.run_intent_id, '',
  ].join('\n'))
}

export async function requestKey(snapshot: RunSnapshot): Promise<string> { return sha256Hex(requestKeyBytes(snapshot)) }

export async function verifySnapshot(snapshot: RunSnapshot, interpreterBindings?: Record<string, unknown>, catalog?: JsonObject): Promise<void> {
  const value = objectOf(snapshot, 'RunSnapshot')
  assertSchema(value, 'run-snapshot/v1')
  assertHash(value.request_key, 'request_key')
  assertHash(value.snapshot_hash, 'snapshot_hash')
  const assets = arrayOf(value.asset_hashes, 'asset_hashes').map(item => objectOf(item, 'asset_hash'))
  assertUnique(assets.map(item => item.asset_id), 'asset_hashes contains duplicate asset IDs')
  const assetMap = new Map(assets.map(item => [String(item.asset_id), String(item.sha256)]))
  for (const asset of assets) assertHash(asset.sha256, 'asset_hashes.sha256')
  if (value.parameters_asset_id != null && !assetMap.has(String(value.parameters_asset_id))) fail('parameters_asset_id is absent from asset_hashes')
  for (const [field, keys] of [['input_revisions', ['document_id', 'revision_id']], ['plugin_releases', ['plugin_id']], ['plugin_settings_revisions', ['plugin_id', 'scope', 'scope_id']], ['asset_hashes', ['asset_id']], ['data_bindings', ['order']], ['skill_releases', ['order']]] as [string, string[]][]) {
    const values = arrayOf(value[field], field).map(item => objectOf(item, field))
    const identities = values.map(item => keys.map(key => String(item[key] ?? '')).join('\0'))
    assertUnique(identities, `${field} contains duplicate identity`)
  }
  for (const field of ['data_bindings', 'skill_releases']) {
    const orders = arrayOf(value[field], field).map(item => Number(objectOf(item, field).order))
    if (orders.some((order, index) => index > 0 && (orders[index - 1] as number) > order)) fail(`${field} order is not ascending`)
  }
  for (const item of arrayOf(value.data_bindings, 'data_bindings').map(item => objectOf(item, 'data_binding'))) {
    if (assetMap.get(String(item.bundle_asset_id)) !== item.bundle_hash) fail('data binding bundle hash is not backed by asset_hashes')
  }
  for (const item of arrayOf(value.skill_releases, 'skill_releases').map(item => objectOf(item, 'skill_release'))) {
    if (item.parameters_asset_id != null && !assetMap.has(String(item.parameters_asset_id))) fail('Skill parameter asset is absent from asset_hashes')
  }
  if (await requestKey(snapshot) !== value.request_key) fail('request_key mismatch')
  const unsigned = { ...value }
  delete unsigned.snapshot_hash
  if (await hashJcs('run-snapshot/v1', normalizedSnapshot(unsigned)) !== value.snapshot_hash) fail('snapshot_hash mismatch')
  if (interpreterBindings != null || catalog != null) {
    if (interpreterBindings == null || catalog == null) fail('Data bindings require catalog and resolved interpreter bindings')
    verifyCatalog(catalog)
    for (const rawBinding of arrayOf(value.data_bindings, 'data_bindings')) {
      const binding = objectOf(rawBinding, 'data binding')
      const resolved = objectOf(interpreterBindings[String(binding.interpreter_binding_id)], 'resolved interpreter binding')
      verifyDataInterpreterBinding(String(binding.format_id), String(resolved.plugin_id), String(resolved.capability_id), catalog)
    }
  }
}

function hashWithoutField(value: JsonObject, field: string, prefix: string): Promise<string> {
  const unsigned = { ...value }
  delete unsigned[field]
  return hashJcs(prefix, unsigned)
}

export async function verifyManifest(manifest: JsonObject): Promise<void> {
  assertSchema(manifest, 'plotpilot-plugin/v1')
  const capabilities = arrayOf(manifest.capabilities, 'capabilities').map(item => objectOf(item, 'capability'))
  assertUnique(capabilities.map(item => item.capability_id), 'manifest capability IDs must be unique')
  for (const capability of capabilities) assertUnique(arrayOf(capability.operations, 'operations'), 'manifest capability operations must be unique')
  const compatibility = objectOf(manifest.compatibility, 'compatibility')
  if (compatibility.core_api !== '>=1.0 <2.0' || compatibility.plugin_rpc !== '1' || compatibility.ui_host !== '1') fail('manifest compatibility is outside the v1 matrix')
  if (manifest.kind === 'data' && arrayOf(manifest.needs, 'needs').length !== 0) fail('data plugin needs must be empty')
  if (manifest.kind === 'data' && ['backend', 'storage', 'ui'].some(key => Object.prototype.hasOwnProperty.call(manifest, key))) fail('data plugin cannot declare backend/storage/UI fields')
  const ui = manifest.ui == null ? null : objectOf(manifest.ui, 'ui')
  if (ui != null) {
    const contributions = arrayOf(ui.contributions, 'ui.contributions').map(item => objectOf(item, 'ui contribution'))
    assertUnique(contributions.map(item => `${String(item.contribution_id)}\0${String(item.slot)}\0${String(item.capability_id)}`), 'UI contribution triples must be unique')
    const capabilityIds = new Set(capabilities.map(item => String(item.capability_id)))
    for (const contribution of contributions) {
      if (!capabilityIds.has(String(contribution.capability_id))) fail('UI contribution references an unknown capability')
    }
  }
}

function catalogInterpreterMap(catalog: JsonObject): Map<string, Set<string>> {
  const result = new Map<string, Set<string>>()
  for (const rawFormat of arrayOf(catalog.data_formats, 'catalog.data_formats')) {
    const format = objectOf(rawFormat, 'data format')
    const formatId = String(format.format_id)
    if (result.has(formatId)) fail(`catalog contains duplicate data format: ${formatId}`)
    const pairs = new Set<string>()
    for (const rawInterpreter of arrayOf(format.interpreters, `interpreters for ${formatId}`)) {
      const interpreter = objectOf(rawInterpreter, 'interpreter')
      const pair = `${String(interpreter.plugin_id)}\0${String(interpreter.capability_id)}`
      if (pairs.has(pair)) fail(`catalog repeats interpreter pair for ${formatId}`)
      pairs.add(pair)
    }
    result.set(formatId, pairs)
  }
  return result
}

export function verifyDataInterpreterBinding(formatId: string, pluginId: string, capabilityId: string, catalog: JsonObject): void {
  assertId(formatId, 'format_id')
  assertId(pluginId, 'interpreter plugin_id')
  assertId(capabilityId, 'interpreter capability_id')
  const expected = catalogInterpreterMap(catalog).get(formatId)
  if (expected == null) fail(`no interpreter for ${formatId}`)
  if (!expected.has(`${pluginId}\0${capabilityId}`)) fail('interpreter capability does not accept the Data format')
}

export function verifyCatalog(catalog: JsonObject): void {
  if (catalog.schema !== 'novel-agent-plugin-catalog/v1') fail('catalog schema mismatch')
  const plugins = arrayOf(catalog.code_plugins, 'catalog.code_plugins').map(item => objectOf(item, 'code plugin'))
  assertUnique(plugins.map(item => item.plugin_id), 'catalog plugin IDs must be unique')
  const slots = new Set(arrayOf(catalog.ui_slots, 'catalog.ui_slots').map(item => String(objectOf(item, 'UI slot').slot_id)))
  if (slots.size !== arrayOf(catalog.ui_slots, 'catalog.ui_slots').length) fail('catalog UI slot IDs must be unique')
  const accepted = new Map<string, Set<string>>()
  const contributions = new Set<string>()
  const capabilityIds: string[] = []
  let planned = 0; let conditional = 0; let duplicate = 0
  for (const rawPlugin of plugins) {
    const plugin = rawPlugin
    const status = String(plugin.status)
    if (!['planned', 'conditional', 'not_planned_duplicate'].includes(status)) fail(`unknown catalog plugin status: ${status}`)
    if (status === 'planned') planned += 1
    if (status === 'conditional') conditional += 1
    if (status === 'not_planned_duplicate') duplicate += 1
    const pluginCapabilities = arrayOf(plugin.capabilities, 'plugin capabilities').map(item => objectOf(item, 'capability'))
    const pluginCapabilityIds = pluginCapabilities.map(item => String(item.capability_id))
    assertUnique(pluginCapabilityIds, 'catalog capability IDs must be unique per plugin')
    const pluginContributionSlots = new Set<string>()
    for (const capability of pluginCapabilities) {
      const pluginId = String(plugin.plugin_id)
      const capabilityId = String(capability.capability_id)
      capabilityIds.push(capabilityId)
      const headless = capability.headless
      if (typeof headless !== 'boolean') fail(`catalog capability headless flag is missing: ${capabilityId}`)
      const rawContributions = arrayOf(capability.ui_contributions, 'ui_contributions').map(item => objectOf(item, 'UI contribution'))
      if (headless && rawContributions.length !== 0) fail(`headless capability declares UI contributions: ${capabilityId}`)
      if (!headless && rawContributions.length === 0) fail(`non-headless capability has no UI contribution: ${capabilityId}`)
      for (const contribution of rawContributions) {
        const slot = String(contribution.slot)
        const triple = `${String(contribution.contribution_id)}\0${slot}\0${String(contribution.capability_id)}`
        if (contributions.has(triple)) fail('duplicate UI contribution triple')
        contributions.add(triple)
        if (String(contribution.capability_id) !== capabilityId) fail('UI contribution capability binding is not its owner')
        if (!slots.has(slot)) fail(`UI contribution uses an unknown slot: ${slot}`)
        pluginContributionSlots.add(slot)
      }
      for (const format of arrayOf(capability.accepted_data_formats, 'accepted_data_formats')) {
        const formatId = String(format)
        const set = accepted.get(formatId) ?? new Set<string>()
        set.add(`${pluginId}\0${capabilityId}`)
        accepted.set(formatId, set)
      }
    }
    const pluginSlots = new Set(arrayOf(plugin.ui_slots, 'plugin.ui_slots').map(item => String(item)))
    if (pluginSlots.size !== pluginContributionSlots.size || [...pluginSlots].some(slot => !pluginContributionSlots.has(slot))) fail(`plugin UI slots do not match contributions: ${String(plugin.plugin_id)}`)
  }
  assertUnique(capabilityIds, 'catalog capability IDs must be globally unique')
  if (catalog.code_plugin_count !== plugins.length || catalog.planned_code_plugin_count !== planned || catalog.conditional_code_plugin_count !== conditional || catalog.not_planned_duplicate_count !== duplicate || catalog.capability_count !== capabilityIds.length) fail('catalog count fields are incorrect')
  const planRules = objectOf(catalog.plan_rules, 'catalog.plan_rules')
  if (canonicalJson(planRules.result_modes) !== canonicalJson(['separate', 'synthesize']) || planRules.separate_requires_null_synthesizer !== true || planRules.synthesize_requires_non_null_synthesizer_resolving_enabled_binding !== true) fail('catalog plan rules are incomplete')
  const resultRules = objectOf(catalog.result_contract_rules, 'catalog.result_contract_rules')
  if (canonicalJson(resultRules.allowed) !== canonicalJson(['artifact-bundle/v1', 'candidate-batch/v1', 'diagnostic-bundle/v1']) || resultRules.failed_or_skipped_create_candidate !== false || canonicalJson(resultRules.failed_attempt_bundle) !== canonicalJson(['null', 'diagnostic-bundle/v1']) || resultRules.stream_is_host_behavior_not_result_contract !== true) fail('catalog result contract rules are incomplete')
  const rebindRules = objectOf(catalog.evidence_rebind_rules, 'catalog.evidence_rebind_rules')
  if (rebindRules.inspect_capability !== 'source.evidence.rebind.inspect/v1' || rebindRules.inspect_result !== 'diagnostic-bundle/v1' || rebindRules.propose_capability !== 'source.evidence.rebind.propose/v1' || rebindRules.propose_result !== 'candidate-batch/v1' || rebindRules.unchanged_scope !== 'same_revision_no_op_only' || canonicalJson(rebindRules.cross_revision_classes) !== canonicalJson(['rebound', 'needs_rerun', 'orphaned'])) fail('catalog evidence rebind rules are incomplete')
  const declared = catalogInterpreterMap(catalog)
  const keys = new Set([...accepted.keys(), ...declared.keys()])
  for (const key of keys) {
    const left = accepted.get(key) ?? new Set<string>(); const right = declared.get(key) ?? new Set<string>()
    if (left.size !== right.size || [...left].some(pair => !right.has(pair))) fail(`Data interpreter mapping is not bidirectional for ${key}`)
  }
}

export function verifyPlan(plan: JsonObject, interpreterBindings?: Record<string, unknown>, catalog?: JsonObject): void {
  assertSchema(plan, 'plugin-plan/v1')
  if (plan.result_mode !== 'separate' && plan.result_mode !== 'synthesize') fail('plugin-plan/v1 result_mode must be separate or synthesize')
  const bindings = arrayOf(plan.bindings, 'bindings').map(item => objectOf(item, 'binding'))
  const dataBindings = arrayOf(plan.data_bindings, 'data_bindings').map(item => objectOf(item, 'data binding'))
  assertUnique(bindings.map(item => item.binding_id), 'binding IDs must be unique')
  assertUnique(dataBindings.map(item => item.data_binding_id), 'data binding IDs must be unique')
  for (const field of [bindings, dataBindings]) {
    const orders = field.map(item => Number(item.order)); assertUnique(orders, 'plan orders must be unique')
    if (orders.some((order, index) => index > 0 && (orders[index - 1] as number) > order)) fail('plan orders must be ascending')
  }
  const synthesizer = plan.synthesizer == null ? null : objectOf(plan.synthesizer, 'synthesizer')
  if (plan.result_mode === 'synthesize') {
    if (synthesizer == null) fail('synthesize plan requires a synthesizer')
    const match = bindings.filter(item => item.binding_id === synthesizer.binding_id)
    if (match.length !== 1 || match[0]?.enabled !== true) fail('synthesizer must resolve to an enabled binding')
    for (const field of ['capability_id', 'plugin_id', 'release_requirement']) if (match[0]?.[field] !== synthesizer[field]) fail('synthesizer identity does not match its binding')
  } else if (synthesizer != null) fail('separate plan must not declare a synthesizer')
  if (interpreterBindings != null || catalog != null) {
    if (catalog == null || interpreterBindings == null) fail('Data bindings require catalog and resolved interpreter bindings')
    verifyCatalog(catalog)
    for (const binding of dataBindings) {
      const resolved = interpreterBindings[String(binding.interpreter_binding_id)]
      const pair = objectOf(resolved, 'resolved interpreter binding')
      verifyDataInterpreterBinding(String(binding.format_id), String(pair.plugin_id), String(pair.capability_id), catalog)
    }
  }
}

export function verifyGeneration(generation: JsonObject): void {
  assertSchema(generation, 'plugin-generation/v1')
  const members = arrayOf(generation.members, 'Generation members').map(item => objectOf(item, 'Generation member'))
  assertUnique(members.map(item => item.plugin_id), 'Generation members must contain one release per plugin')
  if (generation.parent_generation_id === generation.generation_id || generation.base_generation_id === generation.generation_id) fail('Generation cannot parent or base itself')
}

export function verifyGenerationUnchanged(previous: JsonObject, current: JsonObject): void {
  verifyGeneration(previous); verifyGeneration(current)
  if (previous.generation_id !== current.generation_id) fail('Generation identity changed')
  if (canonicalJson(previous) !== canonicalJson(current)) fail('Generation is immutable; current/LKG state must not be written back')
}

const LIFECYCLE_EDGES: Record<string, Set<string>> = {
  selected: new Set(['staged', 'failed', 'superseded']), staged: new Set(['package_published', 'failed', 'superseded']),
  package_published: new Set(['env_prepared', 'failed', 'superseded']), env_prepared: new Set(['shadow_prepared', 'failed', 'superseded']),
  shadow_prepared: new Set(['migrated', 'failed', 'superseded']), migrated: new Set(['settings_validated', 'failed', 'superseded']),
  settings_validated: new Set(['qualified', 'failed', 'superseded']), qualified: new Set(['pending_apply', 'failed', 'superseded']),
  pending_apply: new Set(['current_committed', 'failed', 'superseded']), current_committed: new Set(['lkg_pending', 'rollback_armed', 'failed']),
  lkg_pending: new Set(['lkg_promoted', 'rollback_armed', 'failed']), lkg_promoted: new Set(), rollback_armed: new Set(['rolled_back', 'failed']),
  rolled_back: new Set(['safe_mode']), safe_mode: new Set(), failed: new Set(), superseded: new Set(),
}

const LIFECYCLE_QUALIFIED_STATES = new Set(['qualified', 'pending_apply', 'current_committed', 'lkg_pending', 'lkg_promoted', 'rollback_armed', 'rolled_back', 'safe_mode'])
const LIFECYCLE_SHADOW_STATES = new Set(['shadow_prepared', 'migrated', 'settings_validated', 'qualified', 'pending_apply', 'current_committed', 'lkg_pending', 'lkg_promoted', 'rollback_armed', 'rolled_back', 'safe_mode'])
const LIFECYCLE_ROLLBACK_STATES = new Set(['rollback_armed', 'rolled_back', 'safe_mode'])
const PACKAGE_STORE_ORDER: Record<string, number> = { absent: 0, staged: 1, published: 2, orphan: 3 }

function lifecycleSettingsIdentity(transition: JsonObject): string {
  const settings = arrayOf(transition.target_settings_revision_ids, 'target_settings_revision_ids').map(item => objectOf(item, 'target setting'))
  assertUnique(settings.map(item => item.plugin_id), 'target settings must contain one revision per plugin')
  return canonicalJson([...settings].sort((left, right) => compareUtf8(`${String(left.plugin_id)}\0${String(left.settings_revision_id)}`, `${String(right.plugin_id)}\0${String(right.settings_revision_id)}`)))
}

function lifecycleTime(value: unknown, label: string): number {
  if (typeof value !== 'string') fail(`${label} must be a UTC timestamp`)
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) fail(`${label} must be a valid UTC timestamp`)
  return parsed
}

function verifyLifecycleStateShape(transition: JsonObject): void {
  const state = String(transition.state)
  const target = transition.target_generation_id
  if (['current_committed', 'lkg_pending', 'lkg_promoted', 'rollback_armed', 'rolled_back', 'safe_mode'].includes(state) && target == null) fail(`${state} requires target_generation_id`)
  if (LIFECYCLE_QUALIFIED_STATES.has(state) && transition.qualification_id == null) fail(`${state} requires qualification_id`)
  if (LIFECYCLE_SHADOW_STATES.has(state) && transition.shadow_data_generation_id == null) fail(`${state} requires shadow_data_generation_id`)
  if (state === 'failed' && transition.failure_code == null) fail('failed lifecycle transition requires failure_code')
  if (state !== 'failed' && transition.failure_code != null) fail('non-failed lifecycle transition cannot carry failure_code')
  if (state === 'rollback_armed') {
    if (transition.base_lkg_generation_id == null) fail('rollback_armed requires an exact base LKG Generation')
    if (transition.rollback_token == null || transition.rollback_attempt !== 0) fail('rollback_armed requires a fresh rollback token and zero attempts')
  }
  if (state === 'rolled_back' || state === 'safe_mode') {
    if (transition.base_lkg_generation_id == null) fail(`${state} requires the exact base LKG Generation`)
    if (transition.rollback_attempt !== 1 || transition.rollback_token == null) fail(`${state} requires one rollback attempt and a rollback token`)
    if (transition.package_store_status !== 'published') fail(`${state} requires the LKG package to remain published`)
  }
  if (state === 'lkg_promoted' && transition.rollback_attempt !== 0) fail('LKG promotion is not a Generation rollback')
  if (!LIFECYCLE_ROLLBACK_STATES.has(state) && transition.rollback_token != null) fail('rollback_token is only valid for the rollback action')
}

function verifyLifecycleIdentity(previous: JsonObject, current: JsonObject): void {
  for (const field of ['base_generation_id', 'base_lkg_generation_id'] as const) {
    if (previous[field] !== current[field]) fail(`install operation ${field} changed`)
  }
  if (previous.target_generation_id !== current.target_generation_id) {
    fail('install target_generation_id changed')
  }
  if (lifecycleSettingsIdentity(previous) !== lifecycleSettingsIdentity(current)) fail('install target settings identity changed')
  if (previous.qualification_id !== current.qualification_id && !(previous.qualification_id == null && current.qualification_id != null && LIFECYCLE_QUALIFIED_STATES.has(String(current.state)))) fail('qualification identity changed')
  if (previous.shadow_data_generation_id !== current.shadow_data_generation_id && !(previous.shadow_data_generation_id == null && current.shadow_data_generation_id != null && LIFECYCLE_SHADOW_STATES.has(String(current.state)))) fail('shadow data Generation identity changed')
  if (previous.rollback_token !== current.rollback_token && !(previous.rollback_token == null && current.rollback_token != null && LIFECYCLE_ROLLBACK_STATES.has(String(current.state)))) fail('rollback token identity changed')
}

function verifyLifecycleProgression(previous: JsonObject, current: JsonObject): void {
  const previousState = String(previous.state)
  const state = String(current.state)
  if (state === previousState && ['lkg_promoted', 'rolled_back', 'safe_mode', 'failed', 'superseded'].includes(state)) fail(`terminal lifecycle state ${state} cannot be repeated`)
  if (state !== previousState && !(LIFECYCLE_EDGES[previousState]?.has(state) ?? false)) fail(`illegal lifecycle transition ${previousState} -> ${state}`)
  if (Number(current.rollback_attempt) < Number(previous.rollback_attempt)) fail('rollback attempt moved backwards')
  if (previous.rollback_attempt === 1 && current.rollback_attempt !== 1) fail('completed rollback attempt cannot be cleared')
  if (PACKAGE_STORE_ORDER[String(current.package_store_status)] < PACKAGE_STORE_ORDER[String(previous.package_store_status)]) fail('package-store status moved backwards')
  if (lifecycleTime(current.created_at, 'created_at') !== lifecycleTime(previous.created_at, 'created_at')) fail('install operation created_at changed')
  if (lifecycleTime(current.updated_at, 'updated_at') < lifecycleTime(previous.updated_at, 'updated_at')) fail('lifecycle updated_at moved backwards')
}

export function verifyLifecycleTransition(transition: PluginLifecycleTransition | JsonObject, previous?: PluginLifecycleTransition | JsonObject): void {
  const currentValue = objectOf(transition, 'lifecycle transition')
  assertSchema(currentValue, 'plugin-lifecycle-transition/v1')
  verifyLifecycleStateShape(currentValue)
  if (previous != null) {
    const previousValue = objectOf(previous, 'previous lifecycle transition')
    assertSchema(previousValue, 'plugin-lifecycle-transition/v1')
    verifyLifecycleStateShape(previousValue)
    if (previousValue.install_operation_id !== currentValue.install_operation_id) fail('lifecycle transition changed install operation identity')
    verifyLifecycleIdentity(previousValue, currentValue)
    verifyLifecycleProgression(previousValue, currentValue)
  }
}

export function verifyReleaseRetirement(retirement: JsonObject, previous?: JsonObject): void {
  assertSchema(retirement, 'release-retirement/v1')
  const state = String(retirement.state)
  if (state === 'installed' && (retirement.started_at != null || retirement.completed_at != null)) fail('installed release cannot have retirement timestamps')
  if (state === 'retiring' && (retirement.started_at == null || retirement.completed_at != null || retirement.package_present !== true)) fail('retiring release must retain its package until pins drain')
  if (state === 'retired' && (retirement.started_at == null || retirement.completed_at == null || retirement.package_present !== false)) fail('retired release must have completed retirement and no package')
  if (previous != null) {
    assertSchema(previous, 'release-retirement/v1')
    if (previous.release_id !== retirement.release_id) fail('retirement release identity changed')
    const allowed: Record<string, string[]> = { installed: ['retiring'], retiring: ['retired'], retired: [] }
    if (state !== String(previous.state) && !(allowed[String(previous.state)] ?? []).includes(state)) fail('release retirement state moved backwards')
    if (Number(retirement.retire_epoch) < Number(previous.retire_epoch)) fail('retire epoch moved backwards')
    if (previous.state === 'installed' && state === 'retiring' && Number(retirement.retire_epoch) !== Number(previous.retire_epoch) + 1) fail('retiring must increment retire epoch exactly once')
    if (previous.state === retirement.state && retirement.retire_epoch !== previous.retire_epoch) fail('retire epoch changed without a state transition')
  }
}

export function verifyReleasePin(pin: JsonObject, retirement?: JsonObject): void {
  assertSchema(pin, 'release-pin/v1')
  if (retirement != null) {
    verifyReleaseRetirement(retirement)
    if (retirement.release_id !== pin.release_id) fail('pin targets another release')
    if (retirement.state === 'retired' && pin.released_at == null) fail('retired release cannot retain an active pin')
    if (retirement.state === 'retiring' && pin.released_at == null && Number(pin.retire_epoch) >= Number(retirement.retire_epoch)) fail('new executable pin cannot race a retiring release')
  }
}

export async function verifyDataBundle(bundle: JsonObject): Promise<void> {
  assertSchema(bundle, 'plugin-data-bundle/v1')
  if (await hashWithoutField(bundle, 'bundle_hash', 'plugin-data-bundle/v1') !== bundle.bundle_hash) fail('bundle_hash mismatch')
  const paths = arrayOf(bundle.files, 'files').map(item => normalizeWindowsPath(String(objectOf(item, 'file').path)))
  if (paths.some((path, index) => index > 0 && compareUtf8(paths[index - 1] as string, path) > 0)) fail('data bundle files are not sorted by normalized UTF-8 path')
  assertUnique(paths.map(path => unicodeNfcCasefold(path)), 'data bundle paths must be NFC/casefold-unique')
  assertUnique(arrayOf(bundle.files, 'files').map(item => objectOf(item, 'file').asset_id), 'data bundle asset IDs must be unique')
  if (!paths.includes(normalizeWindowsPath(String(bundle.root_path)))) fail('data bundle root_path is absent from files')
}

export async function verifyEvidenceSpan(span: EvidenceSpan, canonicalText?: string, nodeRange?: { start_codepoint: number; end_codepoint: number }, expectedCanonicalTextHash?: string): Promise<void> {
  const value = objectOf(span, 'EvidenceSpan')
  assertExactKeys(value, ['schema', 'workspace_id', 'document_id', 'revision_id', 'node_id', 'start_codepoint', 'end_codepoint', 'quote', 'quote_hash', 'canonical_text_hash'], 'EvidenceSpan')
  assertSchema(value, 'evidence-span/v1')
  for (const field of ['workspace_id', 'document_id', 'revision_id', 'node_id']) assertId(value[field], `EvidenceSpan.${field}`)
  if (!Number.isInteger(value.start_codepoint) || !Number.isInteger(value.end_codepoint) || Number(value.start_codepoint) < 0 || Number(value.end_codepoint) < Number(value.start_codepoint)) fail('invalid EvidenceSpan range')
  if (typeof value.quote !== 'string') fail('EvidenceSpan quote must be a string')
  assertHash(value.quote_hash, 'quote_hash')
  assertHash(value.canonical_text_hash, 'canonical_text_hash')
  if (await sha256Hex(utf8(value.quote)) !== value.quote_hash) fail('EvidenceSpan quote_hash does not match quote UTF-8 bytes')
  if (expectedCanonicalTextHash != null) {
    assertHash(expectedCanonicalTextHash, 'expectedCanonicalTextHash')
    if (value.canonical_text_hash !== expectedCanonicalTextHash) fail('EvidenceSpan canonical text hash does not match the Revision')
  }
  if (nodeRange != null && (Number(value.start_codepoint) < nodeRange.start_codepoint || Number(value.end_codepoint) > nodeRange.end_codepoint)) fail('EvidenceSpan is outside its Node range')
  if (canonicalText != null) {
    if (Number(value.end_codepoint) > [...canonicalText].length) fail('EvidenceSpan range exceeds canonical text scalar length')
    const scalarSlice = [...canonicalText].slice(Number(value.start_codepoint), Number(value.end_codepoint)).join('')
    if (scalarSlice !== value.quote) fail('EvidenceSpan quote is not the exact canonical scalar slice')
    if (await sha256Hex(utf8(canonicalText)) !== value.canonical_text_hash) fail('EvidenceSpan canonical_text_hash does not match canonical text UTF-8 bytes')
  }
}

const CLAIM_INPUT_FIELDS = ['schema', 'source_revision_id', 'ordered_atoms'] as const
const CLAIM_INPUT_ATOM_FIELDS = ['ordinal', 'atom_id', 'payload_hash', 'acceptance_ordinal', 'evidence_spans'] as const

export async function verifyClaimInputStructure(claimInput: JsonObject, options: ClaimInputStructureOptions = {}): Promise<void> {
  assertExactKeys(claimInput, CLAIM_INPUT_FIELDS, 'claim-input/v1')
  assertSchema(claimInput, 'claim-input/v1')
  assertId(claimInput.source_revision_id, 'claim-input.source_revision_id')
  if (options.expectedSourceRevisionId != null && claimInput.source_revision_id !== options.expectedSourceRevisionId) fail('claim-input source Revision does not match the run')
  const atoms = arrayOf(claimInput.ordered_atoms, 'claim-input.ordered_atoms').map(item => objectOf(item, 'ordered Atom'))
  if (atoms.length === 0) fail('claim-input ordered_atoms must be non-empty')
  const atomIds: string[] = []
  for (const [ordinal, atom] of atoms.entries()) {
    assertExactKeys(atom, CLAIM_INPUT_ATOM_FIELDS, 'claim-input ordered Atom')
    if (atom.ordinal !== ordinal) fail('claim-input Atom ordinals must be contiguous from zero')
    assertId(atom.atom_id, 'claim-input.atom_id')
    assertHash(atom.payload_hash, 'claim-input.payload_hash')
    if (!Number.isInteger(atom.acceptance_ordinal) || Number(atom.acceptance_ordinal) < 1) fail('claim-input acceptance_ordinal must be a positive integer')
    const spans = arrayOf(atom.evidence_spans, 'claim-input.evidence_spans').map(item => objectOf(item, 'EvidenceSpan'))
    if (spans.length === 0) fail('accepted Atom must carry at least one EvidenceSpan')
    for (const span of spans) {
      if (String(span.revision_id) !== String(claimInput.source_revision_id)) fail('EvidenceSpan crosses the claim source Revision')
      await verifyEvidenceSpan(span as unknown as EvidenceSpan, options.canonicalText, undefined, options.expectedCanonicalTextHash)
    }
    atomIds.push(String(atom.atom_id))
  }
  assertUnique(atomIds, 'claim-input Atom IDs must be unique')
}

function resolveAuthoritativeAtom(source: AuthoritativeAtomSource | null | undefined, atomId: string): JsonObject {
  if (source == null) return fail('claim-input sealing requires authoritative Atom records')
  let candidate: JsonObject | null | undefined
  if (typeof source === 'function') {
    candidate = source(atomId)
  } else if (Array.isArray(source)) {
    const matches = source.filter(item => item.atom_id === atomId)
    if (matches.length > 1) fail('claim-input authority contains duplicate Atom records')
    candidate = matches[0]
  } else if (Object.prototype.hasOwnProperty.call(source, 'atom_id')) {
    candidate = source as unknown as AuthoritativeAtom
  } else {
    candidate = (source as Record<string, AuthoritativeAtom>)[atomId]
  }
  if (candidate == null) return fail('claim-input Atom is not an authoritative current Atom')
  return objectOf(candidate, 'authoritative Atom')
}

function verifyClaimInputAuthority(claimInput: JsonObject, source: AuthoritativeAtomSource | null | undefined): void {
  const sourceRevisionId = String(claimInput.source_revision_id)
  const atoms = arrayOf(claimInput.ordered_atoms, 'claim-input.ordered_atoms').map(item => objectOf(item, 'ordered Atom'))
  for (const atom of atoms) {
    const authority = resolveAuthoritativeAtom(source, String(atom.atom_id))
    const required = ['atom_id', 'payload_hash', 'acceptance_ordinal', 'current', 'accepted']
    const missing = required.filter(field => !Object.prototype.hasOwnProperty.call(authority, field))
    if (missing.length !== 0) fail(`authoritative Atom is missing required fields: ${missing.join(',')}`)
    assertId(authority.atom_id, 'authoritative Atom.atom_id')
    assertHash(authority.payload_hash, 'authoritative Atom.payload_hash')
    if (authority.atom_id !== atom.atom_id) fail('authoritative Atom ID does not match claim-input Atom')
    if (authority.current !== true || authority.accepted !== true) fail('claim-input Atom is not current and accepted')
    if (!Number.isInteger(authority.acceptance_ordinal) || Number(authority.acceptance_ordinal) < 1) fail('authoritative Atom acceptance_ordinal must be a positive integer')
    const revisions = ['revision_id', 'source_revision_id']
      .filter(field => Object.prototype.hasOwnProperty.call(authority, field))
      .map(field => authority[field])
    if (revisions.length === 0 || revisions.some(revision => typeof revision !== 'string' || revision !== sourceRevisionId)) fail('authoritative Atom belongs to another source Revision')
    if (authority.payload_hash !== atom.payload_hash) fail('claim-input payload_hash drifted from the authoritative Atom')
    if (authority.acceptance_ordinal !== atom.acceptance_ordinal) fail('claim-input acceptance_ordinal drifted from the authoritative Atom')
  }
}

export async function verifyClaimInput(claimInput: JsonObject, options: ClaimInputValidationOptions = {}): Promise<void> {
  await verifyClaimInputStructure(claimInput, options)
  verifyClaimInputAuthority(claimInput, options.acceptedAtoms)
}

export async function buildClaimInputAsset(sourceRevisionId: string, orderedAtoms: JsonObject[], options: ClaimInputAuthorityOptions): Promise<{ bytes: Uint8Array; assetHash: string }> {
  const value: JsonObject = { schema: 'claim-input/v1', source_revision_id: sourceRevisionId, ordered_atoms: structuredClone(orderedAtoms) }
  await verifyClaimInput(value, options)
  const bytes = canonicalBytes(value)
  return { bytes, assetHash: await sha256Hex(bytes) }
}

export async function verifyClaimInputAsset(rawAssetBytes: Uint8Array, snapshot: RunSnapshot, options: ClaimInputValidationOptions & { assetId?: string } = {}): Promise<JsonObject> {
  await verifySnapshot(snapshot)
  const assetId = snapshot.parameters_asset_id
  if (assetId == null) fail('RunSnapshot has no claim-input parameters Asset')
  if (options.assetId != null && options.assetId !== assetId) fail('claim-input Asset is not RunSnapshot parameters_asset_id')
  const asset = snapshot.asset_hashes.find(item => item.asset_id === assetId)
  if (asset == null) fail('claim-input Asset hash is absent from RunSnapshot asset_hashes')
  if (await sha256Hex(rawAssetBytes) !== asset.sha256) fail('claim-input Asset bytes do not match the RunSnapshot hash')
  const decoded = new TextDecoder('utf-8', { fatal: true }).decode(rawAssetBytes)
  const parsed = objectOf(JSON.parse(decoded), 'claim-input Asset')
  if (canonicalBytes(parsed).some((byte, index) => byte !== (rawAssetBytes[index] ?? -1)) || canonicalBytes(parsed).byteLength !== rawAssetBytes.byteLength) fail('claim-input Asset must be exact RFC 8785 JCS bytes')
  await verifyClaimInput(parsed, options)
  return parsed
}

function verifyCandidate(item: CandidateItem, workspaceId: string): void {
  const value = objectOf(item, 'candidate')
  assertSchema(value, 'candidate-item/v1')
  if (value.status === 'failed' || value.status === 'skipped') fail('failed/skipped result item cannot create or retain a Candidate')
  const target = objectOf(value.target, 'candidate target')
  if (target.workspace_id !== workspaceId) fail('candidate target is outside the snapshot workspace')
  const base = objectOf(value.base, 'candidate base')
  const writes = arrayOf(value.write_set, 'candidate write_set').map(entry => objectOf(entry, 'write_set entry'))
  if (writes.some(entry => entry.workspace_id !== workspaceId)) fail('candidate write_set crosses the snapshot workspace')
  assertUnique(writes.map(entry => `${entry.workspace_id}\0${entry.entity_kind}\0${entry.entity_id}`), 'candidate write_set contains duplicate target identity')
  const targetEntry = writes.find(entry => `${entry.workspace_id}\0${entry.entity_kind}\0${entry.entity_id}` === `${target.workspace_id}\0${target.entity_kind}\0${target.entity_id}`)
  if (targetEntry == null || targetEntry.revision_id !== base.revision_id || targetEntry.content_hash !== base.content_hash) fail('candidate base does not match target write_set entry')
  const mutation = objectOf(value.mutation, 'mutation')
  if (value.item_kind === 'incomplete_stream') {
    if (value.status !== 'partial' || target.entity_kind !== 'document' || mutation.mode !== 'replace') fail('invalid incomplete stream Candidate')
    if (mutation.payload_schema !== 'core/document-text/v1') fail('incomplete_stream must use the Core document-text schema')
    return
  }
  const allowed: Record<string, { itemKind: string; modes: Set<string> }> = {
    document: { itemKind: 'document', modes: new Set(['replace', 'text_patch', 'append_text']) },
    node_structure: { itemKind: 'node_structure', modes: new Set(['structure_patch']) },
    relation_set: { itemKind: 'relation_set', modes: new Set(['relation_patch']) },
  }
  const expected = allowed[String(target.entity_kind)]
  if (expected == null || value.item_kind !== expected.itemKind || !expected.modes.has(String(mutation.mode))) fail('candidate item kind/mutation does not match target')
}

export function verifyCandidateItem(item: CandidateItem | JsonObject, workspaceId?: string | null): void {
  const value = objectOf(item, 'candidate')
  assertSchema(value, 'candidate-item/v1')
  if (workspaceId == null) fail('candidate result verification requires a snapshot workspace')
  verifyCandidate(value as unknown as CandidateItem, workspaceId)
}

function verifyParentGraph(items: JsonObject[], workspaceId: string, knownParentIds?: Iterable<string>): void {
  const graph = new Map<string, Set<string>>()
  for (const item of items) {
    verifyCandidate(item as unknown as CandidateItem, workspaceId)
    graph.set(String(item.item_id), new Set(arrayOf(item.parent_candidate_ids, 'candidate parent IDs').map(parent => String(parent))))
  }
  const known = new Set<string>([...graph.keys(), ...(knownParentIds ?? [])])
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (node: string): void => {
    if (visiting.has(node)) fail('candidate parent cycle detected')
    if (visited.has(node) || !graph.has(node)) return
    visiting.add(node)
    for (const parent of graph.get(node) ?? []) {
      if (!known.has(parent)) fail('candidate parent does not exist')
      visit(parent)
    }
    visiting.delete(node)
    visited.add(node)
  }
  for (const node of graph.keys()) visit(node)
}

function isResultVerificationContext(value: unknown): value is ResultVerificationContext {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  return ['snapshotWorkspaceId', 'snapshotHashValue', 'knownParentIds'].some(field => Object.prototype.hasOwnProperty.call(value, field))
}

function normalizeResultVerificationContext(
  workspaceOrContext?: string | null | ResultVerificationContext,
  knownParentIdsOrSnapshotHash?: Iterable<string> | string | null,
  snapshotHashOrKnownParentIds?: string | null | Iterable<string>,
): ResultVerificationContext {
  if (isResultVerificationContext(workspaceOrContext)) return workspaceOrContext
  let knownParentIds: Iterable<string> | undefined
  let snapshotHashValue: string | null | undefined
  if (typeof knownParentIdsOrSnapshotHash === 'string' || knownParentIdsOrSnapshotHash === null) snapshotHashValue = knownParentIdsOrSnapshotHash
  else knownParentIds = knownParentIdsOrSnapshotHash
  if (typeof snapshotHashOrKnownParentIds === 'string' || snapshotHashOrKnownParentIds === null) snapshotHashValue = snapshotHashOrKnownParentIds
  else if (snapshotHashOrKnownParentIds !== undefined) knownParentIds = snapshotHashOrKnownParentIds
  return { snapshotWorkspaceId: workspaceOrContext, snapshotHashValue, knownParentIds }
}

export function verifyResultProfile(
  bundle: ResultBundle | JsonObject,
  workspaceOrContext?: string | null | ResultVerificationContext,
  knownParentIdsOrSnapshotHash?: Iterable<string> | string | null,
  snapshotHashOrKnownParentIds?: string | null | Iterable<string>,
): void {
  const context = normalizeResultVerificationContext(workspaceOrContext, knownParentIdsOrSnapshotHash, snapshotHashOrKnownParentIds)
  const value = objectOf(bundle, 'result bundle')
  assertSchema(value, 'result-bundle/v1')
  const profile: Record<string, [string, string]> = {
    'candidate-batch/v1': ['candidate_batch', 'candidate-item/v1'],
    'artifact-bundle/v1': ['artifact', 'artifact-item/v1'],
    'diagnostic-bundle/v1': ['diagnostic', 'diagnostic-item/v1'],
  }
  const expected = profile[String(value.contract_id)]
  if (expected == null || value.bundle_type !== expected[0]) fail('result bundle profile mismatch')
  if (expected[1] === 'candidate-item/v1' && context.snapshotWorkspaceId == null) fail('candidate result verification requires a snapshot workspace')
  const items = arrayOf(value.items, 'result items').map(item => objectOf(item, 'result item'))
  assertUnique(items.map(item => item.item_id), 'result item IDs must be unique')
  const itemIds = new Set(items.map(item => String(item.item_id)))
  const incompleteTargets = new Set<string>()
  for (const item of items) {
    if (item.schema !== expected[1]) fail('result bundle item profile does not match contract')
    if (expected[1] === 'candidate-item/v1') {
      verifyCandidate(item as unknown as CandidateItem, context.snapshotWorkspaceId as string)
      if (item.item_kind === 'incomplete_stream') {
        const target = objectOf(item.target, 'incomplete stream target')
        const targetKey = `${String(target.workspace_id)}\0${String(target.entity_kind)}\0${String(target.entity_id)}`
        if (incompleteTargets.has(targetKey)) fail('only one incomplete stream Candidate is allowed per target')
        incompleteTargets.add(targetKey)
      }
    }
  }
  if (expected[1] === 'candidate-item/v1') verifyParentGraph(items, context.snapshotWorkspaceId as string, context.knownParentIds)
  if (context.snapshotHashValue != null && value.input_snapshot_hash !== context.snapshotHashValue) fail('bundle input snapshot does not match current snapshot')
  for (const rawRef of arrayOf(value.skill_chain_result_refs, 'result Skill references')) {
    verifyChainRef(objectOf(rawRef, 'result Skill reference'), false, {
      bundleId: String(value.bundle_id),
      itemIds,
      allowStream: false,
    })
  }
  const statuses = items.map(item => String(item.status))
  if (value.partial === true && !statuses.some(status => ['partial', 'failed', 'skipped'].includes(status))) fail('partial result bundle must expose a non-complete item')
  if (value.partial === false && statuses.some(status => status !== 'complete')) fail('complete result bundle cannot contain partial/failed/skipped items')
}

export function verifyAttemptResult(
  bundle: ResultBundle | JsonObject | null,
  attemptState: string,
  workspaceOrContext?: string | null | ResultVerificationContext,
  knownParentIdsOrSnapshotHash?: Iterable<string> | string | null,
  snapshotHashOrKnownParentIds?: string | null | Iterable<string>,
): void {
  const context = normalizeResultVerificationContext(workspaceOrContext, knownParentIdsOrSnapshotHash, snapshotHashOrKnownParentIds)
  if (attemptState === 'failed' || attemptState === 'skipped') {
    if (bundle == null) return
    const value = objectOf(bundle, 'failed Attempt result')
    if (value.contract_id !== 'diagnostic-bundle/v1' || value.bundle_type !== 'diagnostic') fail('failed/skipped Attempt may only return null or diagnostic-bundle/v1')
    verifyResultProfile(bundle, context)
    return
  }
  if (bundle == null) fail('non-failed Attempt requires a result Bundle')
  verifyResultProfile(bundle, context)
}

function stageReceipt(candidateIds: Iterable<string>, state: CandidateStageReceipt['state']): CandidateStageReceipt {
  return { candidate_ids: [...candidateIds], state }
}

export class CandidateStagingStore {
  readonly workspaceId: string
  _visible: Map<string, CandidateItem>
  readonly _knownParentIds: Set<string>
  _version: number

  constructor(workspaceId: string, visibleCandidates: readonly CandidateItem[] = [], knownParentIds: Iterable<string> = []) {
    if (typeof workspaceId !== 'string' || workspaceId.length === 0) fail('Candidate staging requires a non-empty workspace_id')
    this.workspaceId = workspaceId
    this._visible = new Map()
    this._knownParentIds = new Set(knownParentIds)
    this._version = 0
    for (const candidate of visibleCandidates) {
      const value = structuredClone(candidate)
      verifyCandidateItem(value, workspaceId)
      if (this._visible.has(value.item_id)) fail('visible Candidate IDs must be unique')
      this._visible.set(value.item_id, value)
    }
    verifyParentGraph([...this._visible.values()] as unknown as JsonObject[], workspaceId, this._knownParentIds)
  }

  visibleCandidates(): CandidateItem[] {
    return [...this._visible.values()].map(candidate => structuredClone(candidate))
  }

  visibleCandidateIds(): string[] {
    return [...this._visible.keys()]
  }

  transaction(): CandidateStagingTransaction {
    return new CandidateStagingTransaction(this)
  }

  beginTransaction(): CandidateStagingTransaction {
    return this.transaction()
  }

  stageAndPublish(bundle: ResultBundle | JsonObject | null, attemptState = 'succeeded'): CandidateStageReceipt {
    const transaction = this.transaction()
    try {
      const staged = transaction.stage(bundle, attemptState)
      if (staged.state === 'rolled_back') return staged
      return transaction.publish()
    } catch (error) {
      transaction.rollback()
      throw error
    }
  }
}

export class CandidateStagingTransaction {
  readonly _store: CandidateStagingStore
  readonly _baseVersion: number
  _pending: Map<string, CandidateItem>
  _bundle: JsonObject | null
  _state: CandidateStageReceipt['state'] | 'open'

  constructor(store: CandidateStagingStore) {
    this._store = store
    this._baseVersion = store._version
    this._pending = new Map()
    this._bundle = null
    this._state = 'open'
  }

  private ensureOpen(): void {
    if (this._state !== 'open') fail('Candidate staging transaction is closed')
  }

  stage(bundle: ResultBundle | JsonObject | null, attemptState = 'succeeded'): CandidateStageReceipt {
    this.ensureOpen()
    try {
      verifyAttemptResult(bundle, attemptState, this._store.workspaceId, new Set([...this._store._visible.keys(), ...this._store._knownParentIds]))
      if (bundle == null) return this.rollback()
      const value = objectOf(bundle, 'candidate staging result')
      if (value.contract_id !== 'candidate-batch/v1') return this.rollback()
      const items = arrayOf(value.items, 'candidate staging items').map(item => objectOf(item, 'candidate staging item'))
      this._pending = new Map(items.map(item => [String(item.item_id), structuredClone(item) as unknown as CandidateItem]))
      this._bundle = structuredClone(value)
      return stageReceipt(this._pending.keys(), 'staged')
    } catch (error) {
      this.rollback()
      throw error
    }
  }

  publish(): CandidateStageReceipt {
    this.ensureOpen()
    try {
      if (this._bundle == null) return this.rollback()
      if (this._store._version !== this._baseVersion) fail('Candidate staging base changed before publish')
      verifyResultProfile(this._bundle, this._store.workspaceId, new Set([...this._store._visible.keys(), ...this._store._knownParentIds]))
      const updated = new Map(this._store._visible)
      for (const [candidateId, candidate] of this._pending) {
        const previous = updated.get(candidateId)
        if (previous != null && canonicalJson(previous) !== canonicalJson(candidate)) fail('Candidate ID is already visible with different content')
        updated.set(candidateId, structuredClone(candidate))
      }
      this._store._visible = updated
      this._store._version += 1
      this._state = 'published'
      return stageReceipt(this._pending.keys(), 'published')
    } catch (error) {
      this.rollback()
      throw error
    }
  }

  rollback(): CandidateStageReceipt {
    if (this._state === 'published') fail('published Candidate staging cannot be rolled back')
    this._pending.clear()
    this._bundle = null
    this._state = 'rolled_back'
    return stageReceipt([], 'rolled_back')
  }
}

export async function verifySettingsValidationReceipt(receipt: JsonObject): Promise<void> {
  assertSchema(receipt, 'settings-validation-receipt/v1')
  if (await hashWithoutField(receipt, 'receipt_hash', 'settings-validation-receipt/v1') !== receipt.receipt_hash) fail('settings validation receipt_hash mismatch')
}

export async function verifyProvenanceReceipt(receipt: ProvenanceReceipt | JsonObject): Promise<void> {
  const value = objectOf(receipt, 'provenance receipt')
  assertSchema(value, 'provenance-receipt/v1')
  if ((value.bundle_id == null) !== (value.bundle_hash == null)) fail('provenance Bundle ID/hash must be all-null or all-present')
  const staged = arrayOf(value.staged_items, 'provenance staged_items')
  assertUnique(staged, 'provenance staged item IDs must be unique')
  for (const rawRef of arrayOf(value.skill_chain_result_refs, 'provenance skill_chain_result_refs')) verifyChainRef(objectOf(rawRef, 'provenance Skill reference'), true)
  if (await hashWithoutField(value, 'receipt_hash', 'provenance-receipt/v1') !== value.receipt_hash) fail('provenance receipt_hash mismatch')
}

export async function verifyCheckpoint(checkpoint: Checkpoint | JsonObject, options: { expectedSnapshotHash?: string; previousSeq?: number } = {}): Promise<void> {
  const value = objectOf(checkpoint, 'checkpoint')
  assertSchema(value, 'checkpoint/v1')
  if (options.expectedSnapshotHash != null && value.run_snapshot_hash !== options.expectedSnapshotHash) fail('checkpoint belongs to another RunSnapshot')
  if (options.previousSeq != null && Number(value.checkpoint_seq) <= options.previousSeq) fail('checkpoint sequence moved backwards')
  if (await hashWithoutField(value, 'checkpoint_hash', 'checkpoint/v1') !== value.checkpoint_hash) fail('checkpoint_hash mismatch')
}

export async function verifyStreamPrefix(prefix: StreamPrefix | JsonObject, options: { previous?: StreamPrefix | JsonObject; content?: Uint8Array } = {}): Promise<void> {
  const value = objectOf(prefix, 'stream prefix')
  assertSchema(value, 'stream-prefix/v1')
  if (options.previous != null) {
    const previous = objectOf(options.previous, 'previous stream prefix')
    assertSchema(previous, 'stream-prefix/v1')
    if (value.stream_id !== previous.stream_id || value.attempt_id !== previous.attempt_id || value.lease_epoch !== previous.lease_epoch) fail('stream prefix binding changed')
    if (Number(value.prefix_seq) <= Number(previous.prefix_seq) || Number(value.byte_length) < Number(previous.byte_length)) fail('stream prefix moved backwards')
  }
  if (options.content != null && (options.content.byteLength !== Number(value.byte_length) || await sha256Hex(options.content) !== value.prefix_hash)) fail('stream prefix asset hash/length mismatch')
}

function verifyChainRef(ref: JsonObject, allowBundleless: boolean, options: { bundleId?: string; itemIds?: ReadonlySet<string>; allowStream?: boolean } = {}): void {
  if ((ref.asset_id == null) !== (ref.asset_hash == null)) fail('Skill chain asset ID/hash must be all-null or all-present')
  const bundlePair = ref.result_bundle_id != null && ref.result_item_id != null
  const streamPair = ref.stream_id != null && ref.acked_prefix_hash != null
  if ((ref.result_bundle_id == null) !== (ref.result_item_id == null)) fail('bundle anchor must be all-null or all-present')
  if ((ref.stream_id == null) !== (ref.acked_prefix_hash == null)) fail('stream anchor must be all-null or all-present')
  if (bundlePair && streamPair) fail('Skill chain reference must have exactly one anchor profile')
  if (!bundlePair && !streamPair && !allowBundleless) fail('bundleless Skill reference is not allowed here')
  if (streamPair && options.allowStream === false) fail('result Bundle refs must be bundle-backed')
  if (bundlePair && options.bundleId != null && ref.result_bundle_id !== options.bundleId) fail('Skill reference points at another Bundle')
  if (bundlePair && options.itemIds != null && !options.itemIds.has(String(ref.result_item_id))) fail('Skill reference points at an unknown item')
}

export async function verifySkillReceipt(receipt: SkillRunReceipt | JsonObject): Promise<void> {
  const value = objectOf(receipt, 'Skill receipt')
  assertSchema(value, 'skill-run-receipt/v1')
  verifyChainRef({ result_bundle_id: value.result_bundle_id, result_item_id: value.result_item_id, stream_id: value.stream_id, acked_prefix_hash: value.acked_prefix_hash }, true)
  if (value.result_bundle_id == null && value.stream_id == null && !['failed', 'skipped'].includes(String(value.step_state))) fail('only a failed/skipped Skill receipt may be bundleless')
  if (value.frozen !== true) fail('Skill receipt must be frozen before chain aggregation')
  if (value.step_state === 'skipped' && (value.participated === true || value.verified_patch === true)) fail('skipped Skill step cannot be participated or verified')
  if (value.model_claimed === true && value.claim_evidence_asset_id == null) fail('model_claimed requires claim evidence')
  const patches = arrayOf(value.patches, 'Skill patches').map(item => objectOf(item, 'Skill patch'))
  if (value.verified_patch === true && !patches.some(patch => patch.verified === true)) fail('verified_patch requires a verified patch')
  for (const patch of patches) if (Number(patch.end_codepoint) < Number(patch.start_codepoint)) fail('Skill patch range is inverted')
  if ((value.output_asset_id == null) !== (value.output_hash == null)) fail('Skill output Asset ID/hash must be all-null or all-present')
  if (await hashWithoutField(value, 'receipt_hash', 'skill-run-receipt/v1') !== value.receipt_hash) fail('Skill receipt_hash mismatch')
}

export async function verifySkillChain(chain: SkillChainResult | JsonObject, receipts: Array<SkillRunReceipt | JsonObject>): Promise<void> {
  const value = objectOf(chain, 'Skill chain')
  assertSchema(value, 'skill-chain-result/v1')
  const receiptList = receipts.map(item => objectOf(item, 'Skill receipt')).sort((left, right) => Number(left.chain_index) - Number(right.chain_index))
  assertUnique(receiptList.map(item => item.receipt_id), 'Skill chain receipt IDs must be unique')
  assertUnique(receiptList.map(item => item.chain_index), 'Skill chain receipt indexes must be unique')
  const receiptIds = arrayOf(value.receipt_ids, 'Skill chain receipt_ids')
  if (receiptList.length !== receiptIds.length || receiptList.some((item, index) => item.receipt_id !== receiptIds[index])) fail('Skill chain receipt IDs/indexes are not continuous')
  const chainRef: JsonObject = { result_bundle_id: value.result_bundle_id, result_item_id: value.result_item_id, stream_id: value.stream_id, acked_prefix_hash: value.acked_prefix_hash }
  verifyChainRef(chainRef, true)
  if (value.result_bundle_id == null && value.stream_id == null && !['failed', 'cancelled'].includes(String(value.chain_status))) fail('only a failed/cancelled Skill chain may be bundleless')
  for (const [index, receipt] of receiptList.entries()) {
    await verifySkillReceipt(receipt)
    if (receipt.chain_index !== index || receipt.chain_id !== value.chain_id || receipt.run_snapshot_hash !== value.run_snapshot_hash) fail('Skill chain receipt index/identity mismatch')
    const receiptRef: JsonObject = { result_bundle_id: receipt.result_bundle_id, result_item_id: receipt.result_item_id, stream_id: receipt.stream_id, acked_prefix_hash: receipt.acked_prefix_hash }
    if (!['result_bundle_id', 'result_item_id', 'stream_id', 'acked_prefix_hash'].every(field => receiptRef[field] === chainRef[field])) fail('Skill receipt anchor does not match its chain')
  }
  const receiptHashes = arrayOf(value.receipt_hashes, 'Skill chain receipt_hashes')
  if (receiptHashes.length !== receiptList.length || receiptList.some((item, index) => item.receipt_hash !== receiptHashes[index])) fail('Skill chain receipt hashes do not match')
  if ((value.final_output_asset_id == null) !== (value.final_output_hash == null)) fail('final output asset/hash must be all-null or all-present')
  const finalHash = value.final_output_hash == null ? '-' : String(value.final_output_hash)
  const expected = await sha256Hex(utf8(`skill-chain/v1\n${receiptList.map(item => `${String(item.receipt_hash)}\n`).join('')}${finalHash}\n`))
  if (expected !== value.chain_hash) fail('chain_hash mismatch')
}

export function verifySseRecovery(value: SseRecovery | JsonObject): void {
  const recovery = objectOf(value, 'SSE recovery')
  assertSchema(recovery, 'sse-recovery/v1')
  if (recovery.gap !== recovery.snapshot_required) fail('SSE snapshot_required must equal gap')
  if (recovery.stream_kind === 'core_event' && recovery.aggregate_id != null) fail('core event recovery cannot carry an aggregate job ID')
  if (Number(recovery.requested_after_seq) > Number(recovery.durable_high_water_seq)) fail('SSE cursor is ahead of the durable high-water mark')
  const present = ['snapshot_schema', 'snapshot_revision', 'snapshot_asset_id', 'snapshot_hash'].map(field => recovery[field] != null)
  if ((present.some(Boolean) !== recovery.gap) || (present.some(Boolean) && !present.every(Boolean))) fail('SSE snapshot fields must be all-null or all-present with a gap')
  if (recovery.gap === true && recovery.snapshot_schema !== (recovery.stream_kind === 'core_event' ? 'core-snapshot/v1' : 'job-snapshot/v1')) fail('SSE snapshot schema does not match stream kind')
}

export async function verifyBackup(bundle: BackupBundle): Promise<void> {
  const value = objectOf(bundle, 'backup')
  assertSchema(value, 'backup-bundle/v1')
  if (await hashWithoutField(value, 'bundle_hash', 'plotpilot-backup/v1') !== value.bundle_hash) fail('backup bundle_hash mismatch')
}

export async function verifyPackageIdentity(files: ByteFiles, pluginId: string, version: string, expectedPackageHash: string, expectedReleaseId: string, expectedFilesSha256?: Uint8Array): Promise<void> {
  const digest = await packageDigest(files, pluginId, version)
  if (digest.packageHash !== expectedPackageHash || digest.releaseId !== expectedReleaseId) fail('package identity mismatch')
  if (expectedFilesSha256 != null && canonicalJson([...digest.filesSha256]) !== canonicalJson([...expectedFilesSha256])) fail('files.sha256 mismatch')
}

export async function verifySkillIdentity(files: ByteFiles, skillId: string, version: string, expectedPackageHash: string, expectedReleaseId: string, expectedFilesSha256?: Uint8Array): Promise<void> {
  const digest = await skillPackageDigest(files, skillId, version)
  if (digest.skillPackageHash !== expectedPackageHash || digest.skillReleaseId !== expectedReleaseId) fail('Skill identity mismatch')
  if (expectedFilesSha256 != null && canonicalJson([...digest.filesSha256]) !== canonicalJson([...expectedFilesSha256])) fail('Skill files.sha256 mismatch')
}

export const RPC_METHOD_MATRIX = rpcMethodMatrix.methods

export function validateRpcRequest(request: JsonObject, expectedLeaseEpoch?: number): void {
  const method = String(request.method ?? '')
  const heartbeat = method === 'runtime.heartbeat' && !Object.prototype.hasOwnProperty.call(request, 'id')
  const definition = RPC_METHOD_MATRIX[method]
  if (definition == null || (!heartbeat && method === 'runtime.heartbeat')) fail('unknown or notification-only RPC method')
  if (heartbeat) assertExactKeys(request, ['jsonrpc', 'method', 'meta', 'params'], 'heartbeat')
  else assertExactKeys(request, ['jsonrpc', 'id', 'method', 'meta', 'params'], `${method} request`)
  if (request.jsonrpc !== '2.0') fail('RPC jsonrpc must be 2.0')
  const meta = objectOf(request.meta, 'RPC meta')
  const context = String(meta.context ?? '')
  if (!definition.meta_profile.split('/').includes(context)) fail(`${method} cannot use ${context} meta profile`)
  assertExactKeys(objectOf(request.params, `${method} params`), definition.params.fields, `${method} params`)
  if (expectedLeaseEpoch != null) {
    const actual = context === 'install' ? meta.install_lease_epoch : meta.lease_epoch
    if (actual !== expectedLeaseEpoch) fail('RPC lease epoch is stale')
  }
}

export function validateRpcResult(method: string, result: JsonObject, request?: JsonObject): void {
  const definition = RPC_METHOD_MATRIX[method]
  if (definition == null || method === 'runtime.heartbeat') fail('unknown or notification-only RPC method')
  if (request != null && request.method !== method) fail('RPC result method does not match its request')
  assertExactKeys(result, definition.result.fields, `${method} result`)
  const params = request == null ? {} : objectOf(request.params, `${method} params`)
  if (method === 'runtime.handshake' && result.plugin_protocol !== '1') fail('handshake protocol mismatch')
  if (method === 'runtime.handshake' && request != null && result.release_id !== objectOf(request.meta, 'RPC meta').plugin_release_id) fail('handshake release mismatch')
  if (method === 'host.capability.invoke/v1' && params.expected_result_contract != null && result.child_result_contract !== params.expected_result_contract) fail('child result contract mismatch')
  if (method === 'job.pause' && result.accepted === true && result.checkpoint_asset_id == null) fail('accepted job.pause must return a checkpoint Asset')
}

export function validateRpcResponse(response: JsonObject, method?: string, request?: JsonObject): void {
  if (request != null) {
    if (method == null) method = String(request.method)
    else if (method !== request.method) fail('RPC response method does not match its request')
  }
  if (Object.prototype.hasOwnProperty.call(response, 'error')) {
    const error = objectOf(response.error, 'RPC error')
    if (!Object.prototype.hasOwnProperty.call(rpcMethodMatrix.error_codes, String(error.code))) fail('RPC error code is not in the v1 registry')
  } else {
    const result = objectOf(response.result, 'RPC result')
    if (method != null) validateRpcResult(method, result, request)
  }
}

const COMMON_META = new Set(['protocol_version', 'generation_id', 'plugin_release_id', 'deadline_at', 'context', 'operation_id'])

export function operationContextIdentityProjection(meta: JsonObject, expectedLeaseEpoch?: number): JsonObject {
  const context = String(meta.context ?? '')
  const profileFields = context === 'control' ? [] : context === 'install' ? ['install_operation_id', 'install_lease_epoch'] : context === 'attempt' ? ['job_id', 'step_id', 'attempt_id', 'lease_epoch'] : null
  if (profileFields == null) fail('unknown RPC context profile')
  assertExactKeys(meta, [...COMMON_META, ...profileFields], `${context} RPC meta`)
  if (meta.protocol_version !== '1' || typeof meta.generation_id !== 'string' || !ID.test(meta.generation_id as string) || typeof meta.plugin_release_id !== 'string' || !HASH.test(meta.plugin_release_id as string) || typeof meta.operation_id !== 'string' || !ID.test(meta.operation_id as string) || typeof meta.deadline_at !== 'string' || !UTC.test(meta.deadline_at as string)) fail('invalid operation context meta')
  const projection: JsonObject = { schema: 'operation-context-identity/v1', protocol_version: '1', context, generation_id: meta.generation_id, plugin_release_id: meta.plugin_release_id }
  if (context === 'install') {
    projection.install_operation_id = meta.install_operation_id
    if (!Number.isInteger(meta.install_lease_epoch) || Number(meta.install_lease_epoch) < 1 || expectedLeaseEpoch == null || meta.install_lease_epoch !== expectedLeaseEpoch) fail('install lease epoch is stale')
  } else if (context === 'attempt') {
    for (const field of ['job_id', 'step_id', 'attempt_id']) { if (typeof meta[field] !== 'string' || !ID.test(meta[field] as string)) fail(`invalid ${field}`); projection[field] = meta[field] }
    if (!Number.isInteger(meta.lease_epoch) || Number(meta.lease_epoch) < 1 || expectedLeaseEpoch == null || meta.lease_epoch !== expectedLeaseEpoch) fail('attempt lease epoch is stale')
  } else if (expectedLeaseEpoch != null) fail('control context has no lease epoch')
  return projection
}

export async function deriveOperationContextIdentity(meta: JsonObject, expectedLeaseEpoch?: number): Promise<string> {
  return hashJcs('plotpilot-operation-context/v1', operationContextIdentityProjection(meta, expectedLeaseEpoch))
}

export function canonicalBytes(value: unknown): Uint8Array { return utf8(canonicalJson(value)) }

export { hashJcs, sha256Hex, utf8 }
