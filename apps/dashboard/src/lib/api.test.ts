import { afterEach, describe, expect, it, vi } from 'vitest'

import { CSRF_HEADER, CSRF_VALUE } from '../contract.ts'
import { apiFetch, AuthError, describeError, HttpError, NetworkError, SIGNED_OUT_MESSAGE } from './api.ts'
import { MAX_RETRIES, retryDelay, shouldRetry } from './queryClient.ts'

function respond(status: number, body?: unknown, init: ResponseInit = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(body === undefined ? null : JSON.stringify(body), { status, ...init })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiFetch', () => {
  it('returns the parsed JSON body on 2xx', async () => {
    respond(200, { ok: 1 })
    await expect(apiFetch('/api/snapshot')).resolves.toEqual({ ok: 1 })
  })

  it.each([401, 403])('maps %i to the signed-out AuthError', async (status) => {
    respond(status, { error: 'no session' })
    const err = await apiFetch('/api/snapshot').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(AuthError)
    expect((err as Error).message).toBe('Signed out — reload to sign in via Cloudflare Access')
    expect(describeError(err)).toBe(SIGNED_OUT_MESSAGE)
  })

  it('treats a redirect (Access sending the request to its login page) as signed out', async () => {
    respond(302, undefined, { headers: { Location: 'https://example.cloudflareaccess.com/login' } })
    await expect(apiFetch('/api/snapshot')).rejects.toBeInstanceOf(AuthError)
  })

  it('carries the Worker error message and status on other failures', async () => {
    respond(503, { error: 'snapshot not published yet' })
    const err = (await apiFetch('/api/snapshot').catch((e: unknown) => e)) as HttpError
    expect(err).toBeInstanceOf(HttpError)
    expect(err.status).toBe(503)
    expect(err.message).toBe('snapshot not published yet')
    expect(describeError(err)).toBe('The dashboard API answered 503: snapshot not published yet')
  })

  it('reports an unreachable API as a NetworkError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))))
    const err = await apiFetch('/api/snapshot').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(NetworkError)
    expect(describeError(err)).toBe('Could not reach the dashboard API: Failed to fetch')
  })

  it('sends credentials and stops at redirects on every request', async () => {
    respond(200, {})
    await apiFetch('/api/chart?symbol=ACME')
    const init = vi.mocked(fetch).mock.calls[0][1]!
    expect(init.credentials).toBe('same-origin')
    expect(init.redirect).toBe('manual')
    expect(init.method).toBe('GET')
  })

  it('POSTs JSON with the CSRF header', async () => {
    respond(200, { request: {} })
    await apiFetch('/api/look/approve', { method: 'POST', json: { date: '2026-03-04', approved: true } })
    const init = vi.mocked(fetch).mock.calls[0][1]!
    const headers = init.headers as Record<string, string>
    expect(init.method).toBe('POST')
    expect(headers['Content-Type']).toBe('application/json')
    expect(headers[CSRF_HEADER]).toBe(CSRF_VALUE)
    expect(JSON.parse(String(init.body))).toEqual({ date: '2026-03-04', approved: true })
  })
})

describe('retry policy', () => {
  it('never retries a signed-out or not-yet-published answer', () => {
    expect(shouldRetry(0, new AuthError(401))).toBe(false)
    expect(shouldRetry(0, new HttpError(404, 'chart not published'))).toBe(false)
  })

  it('retries transient failures a bounded number of times', () => {
    expect(shouldRetry(0, new HttpError(503, 'x'))).toBe(true)
    expect(shouldRetry(0, new NetworkError('x'))).toBe(true)
    expect(shouldRetry(MAX_RETRIES, new NetworkError('x'))).toBe(false)
  })

  it('backs off exponentially, capped', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    expect(retryDelay(0)).toBe(1000)
    expect(retryDelay(1)).toBe(2000)
    expect(retryDelay(2)).toBe(4000)
    expect(retryDelay(10)).toBe(20_000)
    vi.restoreAllMocks()
  })
})
