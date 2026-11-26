/**
 * Response helpers shared by every route. Nothing here reads request headers,
 * so nothing here can echo a token back.
 */
import type { ApiError } from '../src/contract.ts'

/**
 * Set on EVERY response the Worker produces (JSON, media, errors). Static
 * assets do not pass through the Worker; their headers come from the assets
 * `_headers` file instead.
 */
export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  // Media, look images and API JSON are only ever loaded by the SPA itself.
  'Cross-Origin-Resource-Policy': 'same-origin',
}

/**
 * CSP for JSON bodies: a JSON response opened directly in a tab can run
 * nothing, load nothing and be framed by nothing. Not set on video/image
 * responses, where `default-src 'none'` would stop the browser's own media
 * viewer from showing a file opened in a tab; those are safe through the
 * forced Content-Type plus nosniff instead.
 */
export const DATA_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; sandbox"

export const JSON_CONTENT_TYPE = 'application/json; charset=utf-8'

export function applySecurityHeaders(headers: Headers): void {
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value)
}

/** Headers for an API JSON body: never cached anywhere. */
export function apiJsonHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  headers.set('Content-Type', JSON_CONTENT_TYPE)
  headers.set('Cache-Control', 'no-store')
  headers.set('Content-Security-Policy', DATA_CSP)
  applySecurityHeaders(headers)
  return headers
}

/** A JSON API response. `body` must already be safe to show the caller. */
export function json(body: unknown, status = 200, extra?: HeadersInit): Response {
  return new Response(JSON.stringify(body), { status, headers: apiJsonHeaders(extra) })
}

/** A JSON error body `{error}` (ApiError). Messages are fixed strings, never request data. */
export function apiError(status: number, error: string, extra?: HeadersInit): Response {
  const body: ApiError = { error }
  return json(body, status, extra)
}

export function methodNotAllowed(allow: readonly string[]): Response {
  return apiError(405, 'method not allowed', { Allow: allow.join(', ') })
}

/** The same response without its body, for HEAD. */
export function withoutBody(res: Response): Response {
  void res.body?.cancel()
  return new Response(null, { status: res.status, statusText: res.statusText, headers: res.headers })
}

/**
 * What to log about an exception: its class and jose error code, never its
 * message (a jose message can quote claims) and never request headers.
 */
export function describeError(err: unknown): string {
  if (err instanceof Error) {
    const code = (err as { code?: unknown }).code
    return typeof code === 'string' ? `${err.name} ${code}` : err.name
  }
  return typeof err
}
