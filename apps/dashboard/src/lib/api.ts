/**
 * The one way the SPA talks to the Worker.
 *
 * Every request is same-origin and carries the Cloudflare Access cookie
 * (`credentials: 'same-origin'`). Failures come back as one of three typed
 * errors so each panel can say exactly what went wrong instead of quietly
 * keeping old numbers on screen:
 *
 *   AuthError     the Access session is gone (401/403, or Access answering a
 *                 background request with a redirect to its login page).
 *                 Retrying cannot fix it; only a full reload signs back in.
 *   HttpError     the Worker answered with another non-2xx status. Its JSON
 *                 `{error}` body, when there is one, becomes the message.
 *   NetworkError  the request never got an answer (offline, DNS, CORS on a
 *                 cross-origin redirect) or the body was not JSON.
 */
import { CSRF_HEADER, CSRF_VALUE, isBuilderError } from '../contract.ts'

export const SIGNED_OUT_MESSAGE = 'Signed out — reload to sign in via Cloudflare Access'

export class AuthError extends Error {
  readonly status: number
  constructor(status: number) {
    super(SIGNED_OUT_MESSAGE)
    this.name = 'AuthError'
    this.status = status
  }
}

export class HttpError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

export class NetworkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

export type ApiFailure = AuthError | HttpError | NetworkError

export function isAuthError(error: unknown): error is AuthError {
  return error instanceof AuthError
}

/** True for a 404: the object has not been published yet. Not worth retrying. */
export function isNotFound(error: unknown): boolean {
  return error instanceof HttpError && error.status === 404
}

/** A short, specific sentence for an error banner or panel note. */
export function describeError(error: unknown): string {
  if (error instanceof AuthError) return SIGNED_OUT_MESSAGE
  if (error instanceof HttpError) return `The dashboard API answered ${error.status}: ${error.message}`
  if (error instanceof NetworkError) return `Could not reach the dashboard API: ${error.message}`
  if (error instanceof Error) return error.message
  return 'Unknown error'
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (isBuilderError(body)) return body.error
  } catch {
    // not JSON; fall through to the status text
  }
  return response.statusText || `HTTP ${response.status}`
}

export interface ApiRequest {
  method?: 'GET' | 'POST'
  /** JSON-serialisable body; sets Content-Type and the CSRF header. */
  json?: unknown
  signal?: AbortSignal
}

/**
 * Fetch a same-origin JSON endpoint. Resolves with the parsed body, or rejects
 * with AuthError / HttpError / NetworkError.
 */
export async function apiFetch<T>(path: string, request: ApiRequest = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  let body: string | undefined
  if (request.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    headers[CSRF_HEADER] = CSRF_VALUE
    body = JSON.stringify(request.json)
  }

  let response: Response
  try {
    response = await fetch(path, {
      method: request.method ?? (body === undefined ? 'GET' : 'POST'),
      headers,
      body,
      credentials: 'same-origin',
      // Access answers an expired session with a redirect to its login page on
      // another origin. Following it would surface as an opaque CORS failure;
      // stopping here lets it be recognised as "signed out".
      redirect: 'manual',
      cache: 'no-store',
      signal: request.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new NetworkError(error instanceof Error ? error.message : String(error))
  }

  const redirected =
    response.type === 'opaqueredirect' || (response.status >= 300 && response.status < 400)
  if (redirected || response.status === 401 || response.status === 403) {
    throw new AuthError(response.status)
  }
  if (!response.ok) {
    throw new HttpError(response.status, await errorMessage(response))
  }
  try {
    return (await response.json()) as T
  } catch {
    throw new NetworkError(`${path} did not return JSON`)
  }
}
