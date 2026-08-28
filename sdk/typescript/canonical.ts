/** RFC 8785-compatible primitives used by the TypeScript SDK. */

/**
 * JSON.stringify and TextEncoder both accept malformed UTF-16 by replacing
 * lone surrogates. RFC 8785 operates on Unicode scalar values, so reject
 * every unpaired code unit before either operation can observe the string.
 */
export function assertWellFormedString(value: string, label = 'string'): void {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index)
    if (codeUnit >= 0xD800 && codeUnit <= 0xDBFF) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : -1
      if (next < 0xDC00 || next > 0xDFFF) throw new Error(`${label} contains an unpaired high surrogate`)
      index += 1
    } else if (codeUnit >= 0xDC00 && codeUnit <= 0xDFFF) {
      throw new Error(`${label} contains an unpaired low surrogate`)
    }
  }
}

export function canonicalJson(value: unknown): string {
  if (value === null) return 'null'
  if (typeof value === 'string') {
    assertWellFormedString(value)
    return JSON.stringify(value)
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('non-finite JSON number is forbidden')
    if (Object.is(value, -0)) return '0'
    const encoded = JSON.stringify(value)
    if (encoded === undefined) throw new Error('number is not JSON serializable')
    return encoded
  }
  if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol' || value === undefined) {
    throw new Error('value is not JSON serializable')
  }
  if (Array.isArray(value)) return `[${value.map(item => canonicalJson(item)).join(',')}]`
  const record = value as Record<string, unknown>
  const fields = Object.keys(record).sort()
  for (const key of fields) assertWellFormedString(key, 'object key')
  return `{${fields.map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`
}

export function utf8(value: string): Uint8Array {
  assertWellFormedString(value)
  return new TextEncoder().encode(value)
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const buffer = new ArrayBuffer(bytes.byteLength)
  new Uint8Array(buffer).set(bytes)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', buffer)
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}

export async function hashJcs(prefix: string, value: unknown): Promise<string> {
  return sha256Hex(utf8(`${prefix}\n${canonicalJson(value)}`))
}
