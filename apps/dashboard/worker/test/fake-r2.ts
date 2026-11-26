/**
 * An in-memory stand-in for the R2 binding, covering the calls the Worker
 * makes: head, get (with `range` and `onlyIf` as an R2Conditional), put. It
 * records every call so tests can prove which keys were (not) touched.
 *
 * Behaviour copied from R2: a failed `onlyIf` returns the metadata without a
 * body; an out-of-bounds range throws (the Worker must never send one).
 */

interface Stored {
  bytes: Uint8Array
  etag: string
  uploaded: Date
  httpMetadata: R2HTTPMetadata
  customMetadata: Record<string, string>
}

export interface R2Call {
  op: 'head' | 'get' | 'put'
  key: string
  options?: unknown
}

/** FNV-1a over the bytes plus a write counter: a different etag per write. */
function etagFor(bytes: Uint8Array, version: number): string {
  let h = 0x811c9dc5
  for (const b of bytes) h = Math.imul(h ^ b, 0x01000193) >>> 0
  return `${h.toString(16).padStart(8, '0')}${version.toString(16).padStart(4, '0')}`
}

async function toBytes(value: unknown): Promise<Uint8Array> {
  if (typeof value === 'string') return new TextEncoder().encode(value)
  if (value instanceof Uint8Array) return value
  if (value instanceof ArrayBuffer) return new Uint8Array(value)
  if (value === null) return new Uint8Array()
  throw new Error('FakeR2Bucket.put: unsupported value type')
}

export class FakeR2Bucket {
  readonly calls: R2Call[] = []
  private readonly store = new Map<string, Stored>()
  private writes = 0
  /** Runs after head() resolves: lets a test replace an object between head and get. */
  afterHead: ((key: string) => void) | null = null

  get binding(): R2Bucket {
    return this as unknown as R2Bucket
  }

  keys(): string[] {
    return [...this.store.keys()].sort()
  }

  /** Seed an object synchronously (JSON values are serialised). */
  seed(key: string, value: string | Uint8Array | object, contentType = 'application/octet-stream'): void {
    const bytes =
      typeof value === 'string'
        ? new TextEncoder().encode(value)
        : value instanceof Uint8Array
          ? value
          : new TextEncoder().encode(JSON.stringify(value))
    this.writes += 1
    this.store.set(key, {
      bytes,
      etag: etagFor(bytes, this.writes),
      uploaded: new Date(Date.UTC(2026, 2, 4, 21, 0, this.writes)),
      httpMetadata: { contentType },
      customMetadata: {},
    })
  }

  text(key: string): string | null {
    const stored = this.store.get(key)
    return stored ? new TextDecoder().decode(stored.bytes) : null
  }

  stored(key: string): Stored | undefined {
    return this.store.get(key)
  }

  private meta(key: string, stored: Stored, range?: R2Range): R2Object {
    const httpMetadata = { ...stored.httpMetadata }
    return {
      key,
      version: stored.etag,
      size: stored.bytes.byteLength,
      etag: stored.etag,
      httpEtag: `"${stored.etag}"`,
      checksums: { toJSON: () => ({}) },
      uploaded: stored.uploaded,
      httpMetadata,
      customMetadata: { ...stored.customMetadata },
      range,
      storageClass: 'Standard',
      writeHttpMetadata(headers: Headers) {
        if (httpMetadata.contentType) headers.set('Content-Type', httpMetadata.contentType)
      },
    } as unknown as R2Object
  }

  async head(key: string): Promise<R2Object | null> {
    this.calls.push({ op: 'head', key })
    const stored = this.store.get(key)
    const result = stored ? this.meta(key, stored) : null
    this.afterHead?.(key)
    return result
  }

  async get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | R2Object | null> {
    this.calls.push({ op: 'get', key, options })
    const stored = this.store.get(key)
    if (!stored) return null

    const onlyIf = options?.onlyIf
    if (onlyIf instanceof Headers) throw new Error('FakeR2Bucket: onlyIf as Headers is not modelled')
    if (onlyIf?.etagMatches !== undefined && onlyIf.etagMatches !== stored.etag) return this.meta(key, stored)
    if (onlyIf?.etagDoesNotMatch !== undefined && onlyIf.etagDoesNotMatch === stored.etag) {
      return this.meta(key, stored)
    }

    const size = stored.bytes.byteLength
    let offset = 0
    let length = size
    const range = options?.range
    if (range instanceof Headers) throw new Error('FakeR2Bucket: range as Headers is not modelled')
    if (range) {
      if ('suffix' in range) {
        length = Math.min(range.suffix, size)
        offset = size - length
      } else {
        offset = range.offset ?? 0
        length = range.length ?? size - offset
      }
      if (offset < 0 || length <= 0 || offset + length > size) {
        throw new Error('get: The requested range is not satisfiable (10039)')
      }
    }
    const slice = stored.bytes.slice(offset, offset + length)
    const meta = this.meta(key, stored, range ? { offset, length } : undefined)
    const body = new Response(slice).body as ReadableStream
    return Object.assign(meta, {
      body,
      bodyUsed: false,
      arrayBuffer: async () => slice.buffer,
      bytes: async () => slice,
      text: async () => new TextDecoder().decode(slice),
      json: async () => JSON.parse(new TextDecoder().decode(slice)),
      blob: async () => new Blob([slice]),
    }) as unknown as R2ObjectBody
  }

  async put(key: string, value: unknown, options?: R2PutOptions): Promise<R2Object> {
    this.calls.push({ op: 'put', key, options })
    const bytes = await toBytes(value)
    this.writes += 1
    const httpMetadata = options?.httpMetadata instanceof Headers ? {} : (options?.httpMetadata ?? {})
    const stored: Stored = {
      bytes,
      etag: etagFor(bytes, this.writes),
      uploaded: new Date(),
      httpMetadata,
      customMetadata: options?.customMetadata ?? {},
    }
    this.store.set(key, stored)
    return this.meta(key, stored)
  }
}
