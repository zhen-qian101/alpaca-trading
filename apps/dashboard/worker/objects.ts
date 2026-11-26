/**
 * Serve one R2 object as an HTTP response: GET and HEAD, a single byte range
 * (206 / 416), and ETag revalidation (304). Video seeking in the SPA depends
 * on the range support.
 *
 * The Content-Type is decided by the caller from the validated path, never
 * taken from the stored object's metadata, and no other stored HTTP metadata
 * (Content-Encoding, Content-Disposition...) is copied onto the response.
 */
import { apiError, applySecurityHeaders } from './http.ts'

export type ByteRange = { start: number; end?: number } | { suffix: number }
export interface ResolvedRange {
  offset: number
  length: number
}

export interface ServeOptions {
  contentType: string
  cacheControl: string
  /** Content-Security-Policy for the response, e.g. DATA_CSP for JSON. None by default. */
  csp?: string
}

/** Digits of a byte position; longer than this is not a real offset. */
const MAX_POS_DIGITS = 15

/**
 * Parse a Range header. Returns null when it is absent, malformed, not in
 * bytes, or asks for more than one range: RFC 9110 §14.2 lets a server ignore
 * such a header and send the whole representation, which is what happens.
 */
export function parseRange(header: string | null): ByteRange | null {
  if (header === null) return null
  const m = /^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$/i.exec(header)
  if (!m) return null
  const [, first, last] = m
  if (first.length > MAX_POS_DIGITS || last.length > MAX_POS_DIGITS) return null
  if (first === '' && last === '') return null
  if (first === '') return { suffix: Number(last) }
  const start = Number(first)
  if (last === '') return { start }
  const end = Number(last)
  if (end < start) return null
  return { start, end }
}

/** Clamp a range to an object of `size` bytes; null when it is unsatisfiable. */
export function resolveRange(range: ByteRange, size: number): ResolvedRange | null {
  if ('suffix' in range) {
    if (range.suffix === 0 || size === 0) return null
    const length = Math.min(range.suffix, size)
    return { offset: size - length, length }
  }
  if (range.start >= size) return null
  const end = Math.min(range.end ?? size - 1, size - 1)
  return { offset: range.start, length: end - range.start + 1 }
}

/** Strip W/ and quotes: the opaque tag R2 compares on. */
function opaqueTag(tag: string): string {
  return tag.trim().replace(/^W\//, '').replace(/^"(.*)"$/, '$1')
}

/** If-None-Match (weak comparison): does any listed tag, or "*", match? */
export function noneMatchHits(header: string, httpEtag: string): boolean {
  const ours = opaqueTag(httpEtag)
  return header.split(',').some((tag) => tag.trim() === '*' || opaqueTag(tag) === ours)
}

/** If-Range: a STRONG etag that matches exactly, or the exact Last-Modified date. */
function ifRangeHolds(header: string, obj: R2Object): boolean {
  const value = header.trim()
  if (value.startsWith('"')) return value === obj.httpEtag
  if (value.startsWith('W/')) return false
  return value === obj.uploaded.toUTCString()
}

/** R2 answers a failed `onlyIf` with the object's metadata and no body. */
function hasBody(obj: R2Object | R2ObjectBody): obj is R2ObjectBody {
  return 'body' in obj
}

function objectHeaders(obj: R2Object, opts: ServeOptions): Headers {
  const headers = new Headers()
  headers.set('Content-Type', opts.contentType)
  headers.set('Cache-Control', opts.cacheControl)
  headers.set('ETag', obj.httpEtag)
  headers.set('Last-Modified', obj.uploaded.toUTCString())
  headers.set('Accept-Ranges', 'bytes')
  if (opts.csp) headers.set('Content-Security-Policy', opts.csp)
  applySecurityHeaders(headers)
  return headers
}

function notModified(obj: R2Object, opts: ServeOptions): Response {
  const headers = objectHeaders(obj, opts)
  headers.delete('Content-Type')
  return new Response(null, { status: 304, headers })
}

function notFound(): Response {
  return apiError(404, 'not found')
}

/**
 * Serve `key` from `bucket` for a GET or HEAD `request`.
 *
 * - Plain GET: one R2 read; If-None-Match with a single tag is pushed down to
 *   R2 (`onlyIf.etagDoesNotMatch`) so a 304 never reads the body.
 * - Range GET and HEAD: read the object's size first, answer 304 / 416 / HEAD
 *   from that, then read exactly the requested bytes, pinned to the same
 *   version with `onlyIf.etagMatches` so Content-Range always describes the
 *   object the bytes came from. If the object was replaced in between, start
 *   over once.
 */
export async function serveObject(
  request: Request,
  bucket: R2Bucket,
  key: string,
  opts: ServeOptions,
): Promise<Response> {
  const isHead = request.method === 'HEAD'
  const range = isHead ? null : parseRange(request.headers.get('Range'))
  const ifNoneMatch = request.headers.get('If-None-Match')

  if (!isHead && range === null) {
    const tags = ifNoneMatch?.split(',').map((t) => t.trim()).filter(Boolean) ?? []
    const pushDown = tags.length === 1 && tags[0] !== '*' ? opaqueTag(tags[0]) : null
    const obj = pushDown
      ? await bucket.get(key, { onlyIf: { etagDoesNotMatch: pushDown } })
      : await bucket.get(key)
    if (!obj) return notFound()
    if (!hasBody(obj)) return notModified(obj, opts)
    if (ifNoneMatch && noneMatchHits(ifNoneMatch, obj.httpEtag)) {
      await obj.body.cancel()
      return notModified(obj, opts)
    }
    const headers = objectHeaders(obj, opts)
    headers.set('Content-Length', String(obj.size))
    return new Response(obj.body, { status: 200, headers })
  }

  for (let attempt = 0; attempt < 2; attempt++) {
    const meta = await bucket.head(key)
    if (!meta) return notFound()
    if (ifNoneMatch && noneMatchHits(ifNoneMatch, meta.httpEtag)) return notModified(meta, opts)

    const headers = objectHeaders(meta, opts)
    if (isHead) {
      headers.set('Content-Length', String(meta.size))
      return new Response(null, { status: 200, headers })
    }

    const ifRange = request.headers.get('If-Range')
    const wanted = range !== null && (ifRange === null || ifRangeHolds(ifRange, meta)) ? range : null
    let slice: ResolvedRange | null = null
    if (wanted !== null) {
      slice = resolveRange(wanted, meta.size)
      if (slice === null) {
        return apiError(416, 'range not satisfiable', {
          'Content-Range': `bytes */${meta.size}`,
          'Accept-Ranges': 'bytes',
        })
      }
    }

    const obj = await bucket.get(key, {
      onlyIf: { etagMatches: meta.etag },
      ...(slice ? { range: { offset: slice.offset, length: slice.length } } : {}),
    })
    if (!obj) return notFound()
    if (!hasBody(obj)) continue // replaced between head and get: look again

    if (slice === null) {
      headers.set('Content-Length', String(obj.size))
      return new Response(obj.body, { status: 200, headers })
    }
    headers.set('Content-Length', String(slice.length))
    headers.set('Content-Range', `bytes ${slice.offset}-${slice.offset + slice.length - 1}/${obj.size}`)
    return new Response(obj.body, { status: 206, headers })
  }
  return apiError(503, 'object changed while being read; retry', { 'Retry-After': '1' })
}
