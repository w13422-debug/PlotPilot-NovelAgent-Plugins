/** RFC 8785-compatible primitives used by the TypeScript SDK. */

export function canonicalJson(value: unknown): string {
  if (value === null) return 'null'
  if (typeof value === 'string') return JSON.stringify(value)
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
  return `{${fields.map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`
}

export function utf8(value: string): Uint8Array {
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
