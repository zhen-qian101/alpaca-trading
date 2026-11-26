/**
 * Strict allowlists for everything a request can turn into an R2 key. A value
 * either matches one of these shapes exactly or the request is refused; no
 * key is ever built from unchecked input, so nothing outside `media/`,
 * `look/`, `chart/`, `snapshot/` and `approvals/` is reachable.
 */
import { R2_KEYS, SYMBOL_RE, type LookAssetKind } from '../src/contract.ts'
import { isCalendarDate } from './dates.ts'
import { JSON_CONTENT_TYPE } from './http.ts'

/** Served media types, by extension. The stored Content-Type is never trusted. */
export const MEDIA_TYPES: Readonly<Record<string, string>> = {
  mp4: 'video/mp4',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  json: JSON_CONTENT_TYPE,
}

/** An archive slot: "video", "slideshow", "slideshow-<label-slug>". */
const SLOT_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
/** A file name: plain ASCII, no leading dot, at most 128 chars. */
const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
/** The only characters a media path may contain. No "%", so no encoded anything. */
const MEDIA_PATH_CHARS_RE = /^[A-Za-z0-9._/-]+$/
const MAX_MEDIA_PATH = 256

export interface MediaTarget {
  /** R2 key, always "media/" + the validated path. */
  key: string
  contentType: string
  /** Lower-case extension, e.g. "mp4". */
  ext: string
}

/**
 * Map the part of a request path after "/media/" to its R2 key.
 *
 * Accepts exactly two shapes:
 *   history.json
 *   <YYYY-MM-DD>/<slot>/<file>.<mp4|png|jpg|jpeg|webp|json>
 *
 * `rest` is the path exactly as the URL parser left it, i.e. still
 * percent-encoded. Any "%" is refused outright, so encoded slashes, dots,
 * backslashes and NULs cannot sneak through, and "." / ".." / empty / dot-led
 * segments are refused even though a parsed URL never contains them.
 */
export function mediaTarget(rest: string): MediaTarget | null {
  if (rest.length === 0 || rest.length > MAX_MEDIA_PATH) return null
  if (!MEDIA_PATH_CHARS_RE.test(rest)) return null
  const segments = rest.split('/')
  if (segments.some((s) => s === '' || s.startsWith('.'))) return null

  let file: string
  if (segments.length === 1) {
    if (segments[0] !== 'history.json') return null
    file = segments[0]
  } else if (segments.length === 3) {
    const [date, slot, name] = segments
    if (!isCalendarDate(date) || !SLOT_RE.test(slot) || !FILE_RE.test(name)) return null
    file = name
  } else {
    return null
  }

  const dot = file.lastIndexOf('.')
  if (dot <= 0) return null
  const ext = file.slice(dot + 1).toLowerCase()
  const contentType = Object.hasOwn(MEDIA_TYPES, ext) ? MEDIA_TYPES[ext] : undefined
  if (!contentType) return null
  return { key: R2_KEYS.mediaPrefix + rest, contentType, ext }
}

/** A trading symbol ("AAPL", "BRK.B", "BTC/USD") or its chart key spelling ("BTC-USD"). */
export function isSymbol(value: string | null): value is string {
  return value !== null && SYMBOL_RE.test(value)
}

const LOOK_KINDS: readonly LookAssetKind[] = ['plate', 'keyframe']

export function isLookAssetKind(value: string | null): value is LookAssetKind {
  return value !== null && (LOOK_KINDS as readonly string[]).includes(value)
}

/** One query parameter, refusing a repeated one (ambiguous) as absent. */
export function singleParam(params: URLSearchParams, name: string): string | null {
  const values = params.getAll(name)
  return values.length === 1 ? values[0] : null
}
