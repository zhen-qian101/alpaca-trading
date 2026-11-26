/**
 * A fetch stand-in that answers the Worker's routes from the synthetic
 * fixtures. Each test can override a route with a status or a body.
 */
import { vi } from 'vitest'

import type { ApproveBody, SnapshotResponse } from '../contract.ts'
import { chartFixtures, historyFixture, snapshotFixture } from './fixtures.ts'

type Answer = { status: number; body?: unknown }
/** A fixed answer, or a function of the request; a function may answer later (a Promise). */
export type Route = Answer | ((init: RequestInit | undefined) => Answer | Promise<Answer>)

export interface MockApi {
  fetch: ReturnType<typeof vi.fn>
  /** Every call as [path, init]. */
  calls: () => [string, RequestInit | undefined][]
  callsTo: (path: string) => [string, RequestInit | undefined][]
}

export function snapshotWith(patch: Partial<SnapshotResponse> = {}): SnapshotResponse {
  return { ...structuredClone(snapshotFixture), ...patch }
}

/** Install the mock as global fetch. `routes` keys are paths (query string ignored except for /api/chart). */
export function mockApi(routes: Record<string, Route> = {}): MockApi {
  // Like the Worker, the default snapshot route merges the stored request.
  let stored: unknown = null
  const defaults: Record<string, Route> = {
    '/api/snapshot': () => ({ status: 200, body: { ...snapshotWith(), ...(stored ? { approval_request: stored } : {}) } }),
    '/media/history.json': { status: 200, body: historyFixture },
    '/api/look/approve': (init) => {
      const body = JSON.parse(String(init?.body)) as ApproveBody
      stored = { ...body, requested_at: '2026-03-04T21:06:00+00:00', requested_by: 'reviewer@example.com' }
      // The Worker answers 202: recorded, applied later by the home box.
      return { status: 202, body: { request: stored } }
    },
  }
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    let key = url.pathname
    if (key === '/api/chart') key = `/api/chart?symbol=${url.searchParams.get('symbol')}`
    let route = routes[key] ?? defaults[key]
    if (!route && url.pathname === '/api/chart') {
      const fixture = chartFixtures[(url.searchParams.get('symbol') ?? '').replaceAll('/', '-')]
      route = fixture ? { status: 200, body: fixture } : { status: 404, body: { error: 'chart not published' } }
    }
    if (!route) route = { status: 404, body: { error: 'not found' } }
    const r = typeof route === 'function' ? await route(init) : route
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fn)
  const calls = () => fn.mock.calls.map(([i, init]) => [new URL(String(i), 'http://localhost').pathname + new URL(String(i), 'http://localhost').search, init] as [string, RequestInit | undefined])
  return { fetch: fn, calls, callsTo: (path) => calls().filter(([p]) => p.startsWith(path)) }
}
