import { FROZEN_SCHEMA_INVENTORY, type FrozenSchema } from './schema-inventory.ts'

/**
 * The Python SDK validates the checked-in Draft 2020-12 files directly.  The
 * TypeScript SDK cannot rely on a runtime schema package (or on a repository
 * checkout being present after installation), so this small validator runs
 * the same frozen schema inventory embedded at build time.  It intentionally
 * implements only keywords present in that inventory, including local
 * `$defs` references and the closed-object/combinator rules used by v1.
 */

export type SchemaValue = Record<string, unknown>

const schemaAliases = new Map<string, FrozenSchema>()

function schemaContractId(filename: string): string {
  return filename.replace(/\.schema\.json$/, '').replace(/-v1$/, '/v1')
}

for (const [filename, schema] of Object.entries(FROZEN_SCHEMA_INVENTORY)) {
  schemaAliases.set(schemaContractId(filename), schema)
  schemaAliases.set(filename, schema)
}

const pluginManifest = FROZEN_SCHEMA_INVENTORY['plugin-manifest-v1.schema.json']
const skillManifest = FROZEN_SCHEMA_INVENTORY['skill-manifest-v1.schema.json']
const resultBundle = FROZEN_SCHEMA_INVENTORY['result-bundle-v1.schema.json']
if (pluginManifest != null) schemaAliases.set('plotpilot-plugin/v1', pluginManifest)
if (skillManifest != null) schemaAliases.set('plotpilot-skill/v1', skillManifest)
if (resultBundle != null) {
  schemaAliases.set('candidate-batch/v1', resultBundle)
  schemaAliases.set('artifact-bundle/v1', resultBundle)
  schemaAliases.set('diagnostic-bundle/v1', resultBundle)
}

function isObject(value: unknown): value is SchemaValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function schemaObject(value: unknown, label: string): SchemaValue {
  if (!isObject(value)) throw new Error(`${label} must be a schema object`)
  return value
}

function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true
  if (typeof left !== typeof right || left === null || right === null) return false
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false
    return left.every((item, index) => deepEqual(item, right[index]))
  }
  if (isObject(left) && isObject(right)) {
    const leftKeys = Object.keys(left).sort()
    const rightKeys = Object.keys(right).sort()
    return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && deepEqual(left[key], right[key]))
  }
  return false
}

function jsonLength(value: string): number {
  // JSON Schema string length is counted in Unicode scalar values, not UTF-16
  // code units.  This is also the length profile used by the Python validator.
  return [...value].length
}

function jsonTypeMatches(value: unknown, type: string): boolean {
  switch (type) {
    case 'null': return value === null
    case 'boolean': return typeof value === 'boolean'
    case 'object': return isObject(value)
    case 'array': return Array.isArray(value)
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number' && Number.isFinite(value)
    case 'integer': return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value)
    default: return false
  }
}

function pointer(schema: FrozenSchema, reference: string): unknown {
  if (!reference.startsWith('#/')) throw new Error(`unsupported schema reference: ${reference}`)
  const parts = reference.slice(2).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))
  let current: unknown = schema
  for (const part of parts) {
    if (!isObject(current) || !Object.prototype.hasOwnProperty.call(current, part)) throw new Error(`unresolved schema reference: ${reference}`)
    current = current[part]
  }
  return current
}

function expectedKeys(schema: SchemaValue): Set<string> | null {
  const properties = isObject(schema.properties) ? schema.properties : null
  if (properties != null) return new Set(Object.keys(properties))
  return null
}

function validateSchema(schema: FrozenSchema, value: unknown, path: string, root: FrozenSchema, issues: string[]): void {
  if (typeof schema.$ref === 'string') {
    validateSchema(schemaObject(pointer(root, schema.$ref), schema.$ref), value, path, root, issues)
  }

  const allOf = Array.isArray(schema.allOf) ? schema.allOf : []
  for (const [index, branch] of allOf.entries()) {
    validateSchema(schemaObject(branch, `${path}.allOf[${index}]`), value, path, root, issues)
  }

  const anyOf = Array.isArray(schema.anyOf) ? schema.anyOf : []
  if (anyOf.length > 0) {
    const validBranches = anyOf.filter(branch => {
      const branchIssues: string[] = []
      validateSchema(schemaObject(branch, `${path}.anyOf`), value, path, root, branchIssues)
      return branchIssues.length === 0
    }).length
    if (validBranches === 0) issues.push(`${path}: must match at least one anyOf branch`)
  }

  const oneOf = Array.isArray(schema.oneOf) ? schema.oneOf : []
  if (oneOf.length > 0) {
    const validBranches = oneOf.filter(branch => {
      const branchIssues: string[] = []
      validateSchema(schemaObject(branch, `${path}.oneOf`), value, path, root, branchIssues)
      return branchIssues.length === 0
    }).length
    if (validBranches !== 1) issues.push(`${path}: must match exactly one oneOf branch`)
  }

  if (Object.prototype.hasOwnProperty.call(schema, 'const') && !deepEqual(value, schema.const)) {
    issues.push(`${path}: must equal const`)
  }
  if (Array.isArray(schema.enum) && !schema.enum.some(candidate => deepEqual(value, candidate))) {
    issues.push(`${path}: must be one of enum values`)
  }

  const types = Array.isArray(schema.type) ? schema.type.map(String) : typeof schema.type === 'string' ? [schema.type] : []
  if (types.length > 0 && !types.some(type => jsonTypeMatches(value, type))) {
    issues.push(`${path}: type mismatch`)
    return
  }

  if (typeof value === 'string') {
    const length = jsonLength(value)
    if (typeof schema.minLength === 'number' && length < schema.minLength) issues.push(`${path}: string shorter than minLength`)
    if (typeof schema.maxLength === 'number' && length > schema.maxLength) issues.push(`${path}: string longer than maxLength`)
    if (typeof schema.pattern === 'string') {
      let matches = false
      try { matches = new RegExp(schema.pattern).test(value) } catch (error) { throw new Error(`invalid frozen schema pattern: ${String(error)}`) }
      if (!matches) issues.push(`${path}: pattern mismatch`)
    }
  }

  if (typeof value === 'number' && Number.isFinite(value)) {
    if (typeof schema.minimum === 'number' && value < schema.minimum) issues.push(`${path}: below minimum`)
    if (typeof schema.maximum === 'number' && value > schema.maximum) issues.push(`${path}: above maximum`)
    if (typeof schema.exclusiveMinimum === 'number' && value <= schema.exclusiveMinimum) issues.push(`${path}: below exclusiveMinimum`)
    if (typeof schema.exclusiveMaximum === 'number' && value >= schema.exclusiveMaximum) issues.push(`${path}: above exclusiveMaximum`)
  }

  if (Array.isArray(value)) {
    if (typeof schema.minItems === 'number' && value.length < schema.minItems) issues.push(`${path}: fewer than minItems`)
    if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) issues.push(`${path}: more than maxItems`)
    if (schema.uniqueItems === true) {
      for (let index = 0; index < value.length; index += 1) {
        if (value.slice(0, index).some(item => deepEqual(item, value[index]))) {
          issues.push(`${path}: uniqueItems violated`)
          break
        }
      }
    }
    if (schema.items != null) {
      const itemSchema = schemaObject(schema.items, `${path}.items`)
      value.forEach((item, index) => validateSchema(itemSchema, item, `${path}/${index}`, root, issues))
    }
  }

  if (isObject(value)) {
    const properties = isObject(schema.properties) ? schema.properties : {}
    const required = Array.isArray(schema.required) ? schema.required.map(String) : []
    for (const key of required) if (!Object.prototype.hasOwnProperty.call(value, key)) issues.push(`${path}: missing required property ${key}`)
    for (const [key, child] of Object.entries(properties)) {
      if (Object.prototype.hasOwnProperty.call(value, key)) validateSchema(schemaObject(child, `${path}.${key}`), value[key], `${path}/${key}`, root, issues)
    }

    const additional = schema.additionalProperties
    if (additional === false) {
      const allowed = new Set(Object.keys(properties))
      for (const key of Object.keys(value)) if (!allowed.has(key)) issues.push(`${path}: additional property ${key}`)
    } else if (isObject(additional)) {
      const allowed = new Set(Object.keys(properties))
      for (const [key, child] of Object.entries(value)) {
        if (!allowed.has(key)) validateSchema(additional, child, `${path}/${key}`, root, issues)
      }
    }

    // The frozen root union schemas use unevaluatedProperties:false around
    // closed oneOf branches.  Branch-level additionalProperties checks above
    // reject unknown keys; for a direct object schema this adds the same
    // closed-key rule when no property schema was supplied.
    if (schema.unevaluatedProperties === false && oneOf.length === 0 && expectedKeys(schema) == null) {
      if (Object.keys(value).length > 0) issues.push(`${path}: unevaluated properties are not allowed`)
    }
  }
}

function schemaFor(contractId: string): FrozenSchema {
  const schema = schemaAliases.get(contractId)
  if (schema == null) throw new Error(`unknown contract schema: ${contractId}`)
  return schema
}

export function schemaErrors(contractId: string, value: unknown): string[] {
  const schema = schemaFor(contractId)
  const issues: string[] = []
  validateSchema(schema, value, '$', schema, issues)
  return issues
}

export function assertValidContract(contractId: string, value: unknown): void {
  const issues = schemaErrors(contractId, value)
  if (issues.length > 0) throw new Error(`${contractId}: ${issues[0]}`)
}

export function frozenSchemaInventory(): readonly string[] {
  return Object.freeze([...Object.keys(FROZEN_SCHEMA_INVENTORY)].sort())
}
