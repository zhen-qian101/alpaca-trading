import { describe, expect, it, vi } from 'vitest'

import fixture from '../../fixtures/portfolio.json'
import { DATA_URL, loadPortfolio } from './load.ts'

const respond = (body: string, init: ResponseInit) => vi.fn(async () => new Response(body, init))

describe('loadPortfolio', () => {
  it('fetches the same-origin file and validates it', async () => {
    const fetchImpl = respond(JSON.stringify(fixture), { status: 200, headers: { 'content-type': 'application/json' } })
    const state = await loadPortfolio(fetchImpl as unknown as typeof fetch)
    expect(state.status).toBe('ready')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect((fetchImpl.mock.calls[0] as unknown[])[0]).toBe(DATA_URL)
    expect(DATA_URL.startsWith('/')).toBe(true)
  })

  it('treats a 404 as not published', async () => {
    const state = await loadPortfolio(respond('not found', { status: 404 }) as unknown as typeof fetch)
    expect(state.status).toBe('missing')
  })

  it('treats the single-page fallback (index.html) as not published', async () => {
    const html = '<!doctype html><html></html>'
    const state = await loadPortfolio(
      respond(html, { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } }) as unknown as typeof fetch,
    )
    expect(state.status).toBe('missing')
  })

  it('treats a network failure as not published', async () => {
    const state = await loadPortfolio(vi.fn(async () => Promise.reject(new TypeError('offline'))) as unknown as typeof fetch)
    expect(state.status).toBe('missing')
  })

  it('reports broken JSON as invalid', async () => {
    const state = await loadPortfolio(
      respond('{"meta":', { status: 200, headers: { 'content-type': 'application/json' } }) as unknown as typeof fetch,
    )
    expect(state.status).toBe('invalid')
  })

  it('refuses a file with a field the schema does not allow', async () => {
    const doc = { ...structuredClone(fixture), research_notes: 'model text' }
    const state = await loadPortfolio(
      respond(JSON.stringify(doc), { status: 200, headers: { 'content-type': 'application/json' } }) as unknown as typeof fetch,
    )
    expect(state.status).toBe('invalid')
  })
})
