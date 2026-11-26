/**
 * The Worker's routes. Every handler here runs only AFTER authenticate()
 * succeeded (see index.ts). Each one reads a fixed R2 key built from a
 * validated value; the approve route is the single write.
 */
import {
  CSRF_HEADER,
  CSRF_VALUE,
  R2_KEYS,
  type ApprovalRequest,
  type ApproveResponse,
  type IsoDate,
} from '../src/contract.ts'
import { dayNumber, inApprovalWindow, isoUtc } from './dates.ts'
import { apiError, apiJsonHeaders, DATA_CSP, json } from './http.ts'
import { serveObject } from './objects.ts'
import { isLookAssetKind, isSymbol, mediaTarget, singleParam } from './validate.ts'

/** The approve body is two fields; anything near this size is not one. */
const MAX_APPROVE_BODY_BYTES = 1024

/**
 * A media binary under a versioned URL. history.json gives every video and
 * cover `?v=<first 12 hex of its SHA-256>` (web/publisher.py media_url), so new
 * bytes always arrive under a new URL and an hour in the browser cache can
 * never replay an old render.
 */
const CACHE_MEDIA_BINARY = 'private, max-age=3600'
/**
 * JSON indexes, look images and any media binary asked for WITHOUT a version
 * can change in place (a re-render keeps its file names): always revalidate,
 * a cheap 304 by ETag.
 */
const CACHE_REVALIDATE = 'private, no-cache'
/** The content version the publisher appends to media URLs. */
const MEDIA_VERSION_RE = /^[0-9a-f]{12}$/

function isVersioned(url: URL): boolean {
  const v = singleParam(url.searchParams, 'v')
  return v !== null && MEDIA_VERSION_RE.test(v)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A stored approval request, or null if it is missing fields or for another date. */
export function asApprovalRequest(value: unknown, date: IsoDate): ApprovalRequest | null {
  if (!isRecord(value)) return null
  const { approved, requested_at, requested_by } = value
  if (value.date !== date || typeof approved !== 'boolean') return null
  if (typeof requested_at !== 'string' || typeof requested_by !== 'string') return null
  return { date, approved, requested_at, requested_by }
}

async function readApproval(bucket: R2Bucket, date: IsoDate): Promise<ApprovalRequest | null> {
  const obj = await bucket.get(R2_KEYS.approval(date))
  if (!obj) return null
  try {
    const request = asApprovalRequest(JSON.parse(await obj.text()), date)
    if (request === null) console.warn(`approvals/${date}.json does not match the contract; ignored`)
    return request
  } catch {
    console.warn(`approvals/${date}.json is not valid JSON; ignored`)
    return null
  }
}

/** GET /api/snapshot: snapshot/latest.json + `approval_request` for its look date. */
export async function getSnapshot(env: Env): Promise<Response> {
  const obj = await env.DATA.get(R2_KEYS.snapshot)
  if (!obj) return apiError(404, 'no snapshot has been published yet')
  let snapshot: unknown
  try {
    snapshot = JSON.parse(await obj.text())
  } catch {
    return apiError(502, 'the published snapshot is not valid JSON')
  }
  if (!isRecord(snapshot)) return apiError(502, 'the published snapshot is not a JSON object')

  const look = snapshot.look
  const lookDate = isRecord(look) && typeof look.date === 'string' && dayNumber(look.date) !== null ? look.date : null
  const approval_request = lookDate === null ? null : await readApproval(env.DATA, lookDate)
  return json({ ...snapshot, approval_request })
}

/** GET /api/chart?symbol=SYM: chart/<chartKey(SYM)>.json, passed through unparsed. */
export async function getChart(env: Env, url: URL): Promise<Response> {
  const symbol = singleParam(url.searchParams, 'symbol')
  if (!isSymbol(symbol)) return apiError(400, 'symbol must be one trading symbol, e.g. AAPL or BTC/USD')
  const obj = await env.DATA.get(R2_KEYS.chart(symbol))
  if (!obj) return apiError(404, 'no chart has been published for this symbol')
  return new Response(obj.body, { status: 200, headers: apiJsonHeaders() })
}

/** GET|HEAD /api/look/asset?date=YYYY-MM-DD&kind=plate|keyframe */
export async function getLookAsset(request: Request, env: Env, url: URL): Promise<Response> {
  const date = singleParam(url.searchParams, 'date')
  const kind = singleParam(url.searchParams, 'kind')
  if (date === null || dayNumber(date) === null) return apiError(400, 'date must be YYYY-MM-DD')
  if (!isLookAssetKind(kind)) return apiError(400, 'kind must be plate or keyframe')
  return serveObject(request, env.DATA, R2_KEYS.lookAsset(date, kind), {
    contentType: 'image/png',
    cacheControl: CACHE_REVALIDATE,
  })
}

/**
 * GET|HEAD /media/<path>: archived recap media from R2, Range-capable. The
 * object is picked by the path alone; a `v` query parameter only decides how
 * long the browser may keep it.
 */
export async function getMedia(request: Request, env: Env, url: URL): Promise<Response> {
  const target = mediaTarget(url.pathname.slice('/media/'.length))
  if (!target) return apiError(400, 'invalid media path')
  const isJson = target.ext === 'json'
  return serveObject(request, env.DATA, target.key, {
    contentType: target.contentType,
    cacheControl: !isJson && isVersioned(url) ? CACHE_MEDIA_BINARY : CACHE_REVALIDATE,
    ...(isJson ? { csp: DATA_CSP } : {}),
  })
}

/** Read at most `limit` bytes of the body; null when it is longer. */
async function readBodyLimited(request: Request, limit: number): Promise<string | null> {
  const declared = request.headers.get('Content-Length')
  if (declared !== null && !(Number(declared) <= limit)) return null
  if (!request.body) return ''
  const reader = request.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > limit) {
      await reader.cancel()
      return null
    }
    chunks.push(value)
  }
  const bytes = new Uint8Array(total)
  let at = 0
  for (const chunk of chunks) {
    bytes.set(chunk, at)
    at += chunk.byteLength
  }
  return new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)
}

function isJsonContentType(value: string | null): boolean {
  return value !== null && value.split(';')[0].trim().toLowerCase() === 'application/json'
}

/**
 * Cross-site request checks for the one write. The custom header alone
 * already forces a CORS preflight that this Worker never answers; Origin and
 * Sec-Fetch-Site are checked as well when the browser sends them.
 */
function crossSiteRefusal(request: Request, url: URL): Response | null {
  if (request.headers.get(CSRF_HEADER) !== CSRF_VALUE) {
    return apiError(403, `missing ${CSRF_HEADER} header`)
  }
  const origin = request.headers.get('Origin')
  if (origin !== null && origin !== url.origin) return apiError(403, 'cross-origin request refused')
  const site = request.headers.get('Sec-Fetch-Site')
  if (site !== null && site !== 'same-origin') return apiError(403, 'cross-site request refused')
  return null
}

/**
 * POST /api/look/approve {date, approved}: record a sign-off request in
 * approvals/<date>.json for the home box to apply. 202: accepted, not applied.
 */
export async function postApprove(
  request: Request,
  env: Env,
  url: URL,
  identity: string | null,
  now: Date,
): Promise<Response> {
  const refused = crossSiteRefusal(request, url)
  if (refused) return refused
  if (!isJsonContentType(request.headers.get('Content-Type'))) {
    return apiError(415, 'Content-Type must be application/json')
  }
  if (identity === null) return apiError(403, 'the Access token carries no email or service-token name')

  let text: string | null
  try {
    text = await readBodyLimited(request, MAX_APPROVE_BODY_BYTES)
  } catch {
    return apiError(400, 'body is not valid UTF-8')
  }
  if (text === null) return apiError(413, 'body too large')

  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return apiError(400, 'body is not valid JSON')
  }
  if (!isRecord(body)) return apiError(400, 'body must be {"date": "YYYY-MM-DD", "approved": true|false}')
  const extra = Object.keys(body).filter((k) => k !== 'date' && k !== 'approved')
  if (extra.length > 0) return apiError(400, 'body may only contain "date" and "approved"')
  const { date, approved } = body
  if (typeof approved !== 'boolean') return apiError(400, '"approved" must be true or false')
  if (typeof date !== 'string' || dayNumber(date) === null) return apiError(400, '"date" must be a real YYYY-MM-DD date')
  if (!inApprovalWindow(date, now)) {
    return apiError(422, 'date is outside the approval window (US/Eastern today -1 to +3 days)')
  }

  const record: ApprovalRequest = {
    date,
    approved,
    requested_at: isoUtc(now),
    requested_by: identity,
  }
  await env.DATA.put(R2_KEYS.approval(date), JSON.stringify(record), {
    httpMetadata: { contentType: 'application/json' },
  })
  const response: ApproveResponse = { request: record }
  return json(response, 202)
}
