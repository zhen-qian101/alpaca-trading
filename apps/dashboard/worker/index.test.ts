// Routing, the read routes (snapshot, chart, look asset) and the headers every
// response carries. Runs in Node: the handler only uses web-standard APIs.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import approvalJson from '../fixtures/approval-request.json' with { type: 'json' }
import acmeJson from '../fixtures/chart-ACME.json' with { type: 'json' }
import qbitJson from '../fixtures/chart-QBIT-USD.json' with { type: 'json' }
import snapshotJson from '../fixtures/snapshot.json' with { type: 'json' }
import type { ChartData, SnapshotResponse } from '../src/contract.ts'
import { accessEnv, makeSigner, mint, serveCerts, TEAM_DOMAIN, type Signer } from './test/access.ts'
import { call, type CallInit } from './test/call.ts'
import { FakeR2Bucket } from './test/fake-r2.ts'

const LOOK_DATE = snapshotJson.look.date // 2026-03-04
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4])

let signer: Signer
let bucket: FakeR2Bucket
let env: Env

beforeAll(async () => {
  signer = await makeSigner('index-key')
})

beforeEach(() => {
  serveCerts({ [TEAM_DOMAIN]: { keys: [signer.jwk] } })
  bucket = new FakeR2Bucket()
  bucket.seed('snapshot/latest.json', snapshotJson, 'application/json')
  bucket.seed('chart/ACME.json', acmeJson, 'application/json')
  bucket.seed('chart/QBIT-USD.json', qbitJson, 'application/json')
  bucket.seed(`look/${LOOK_DATE}/plate.png`, PNG, 'image/png')
  bucket.seed(`look/${LOOK_DATE}/keyframe.png`, PNG.slice(0, 10), 'text/html') // stored type is ignored
  env = accessEnv(bucket.binding)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function authed(path: string, init: CallInit = {}): Promise<Response> {
  return call(env, path, { ...init, token: await mint(signer, { email: 'viewer@example.com' }) })
}

function expectSecurityHeaders(res: Response): void {
  expect(res.headers.get('X-Content-Type-Options')).toBe('nosniff')
  expect(res.headers.get('Referrer-Policy')).toBe('no-referrer')
  expect(res.headers.get('X-Frame-Options')).toBe('DENY')
  expect(res.headers.get('Cross-Origin-Resource-Policy')).toBe('same-origin')
  expect(res.headers.get('Access-Control-Allow-Origin')).toBeNull()
}

function expectApiJson(res: Response): void {
  expectSecurityHeaders(res)
  expect(res.headers.get('Content-Type')).toBe('application/json; charset=utf-8')
  expect(res.headers.get('Cache-Control')).toBe('no-store')
  expect(res.headers.get('Content-Security-Policy')).toContain("default-src 'none'")
}

const readKeys = () => bucket.calls.map((c) => `${c.op} ${c.key}`)

async function snapshotBody(): Promise<SnapshotResponse> {
  return (await authed('/api/snapshot')).json<SnapshotResponse>()
}

describe('GET /api/snapshot', () => {
  it('returns the published snapshot with approval_request null when none exists', async () => {
    const res = await authed('/api/snapshot')
    expect(res.status).toBe(200)
    expectApiJson(res)
    expect(await res.json()).toEqual({ ...snapshotJson, approval_request: null })
    expect(readKeys()).toEqual(['get snapshot/latest.json', `get approvals/${LOOK_DATE}.json`])
  })

  it('merges the approval request for the look date', async () => {
    bucket.seed(`approvals/${LOOK_DATE}.json`, approvalJson, 'application/json')
    const body = await snapshotBody()
    expect(body.approval_request).toEqual(approvalJson)
    expect(body.look).toEqual(snapshotJson.look)
  })

  it('ignores an approval stored for the date but naming another, or malformed', async () => {
    for (const stored of [
      { ...approvalJson, date: '2026-03-05' },
      { ...approvalJson, approved: 'yes' },
      { date: LOOK_DATE, approved: true },
      '{not json',
      '[]',
    ]) {
      bucket.seed(`approvals/${LOOK_DATE}.json`, stored)
      vi.spyOn(console, 'warn').mockImplementation(() => {})
      const body = await snapshotBody()
      expect(body.approval_request).toBeNull()
    }
  })

  it('reads no approval when the look section is a builder error', async () => {
    bucket.seed('snapshot/latest.json', { ...snapshotJson, look: { error: 'look store unavailable' } })
    const body = await snapshotBody()
    expect(body.look).toEqual({ error: 'look store unavailable' })
    expect(body.approval_request).toBeNull()
    expect(readKeys()).toEqual(['get snapshot/latest.json'])
  })

  it('never builds an approvals key from a malformed look date', async () => {
    bucket.seed('snapshot/latest.json', { ...snapshotJson, look: { ...snapshotJson.look, date: '../snapshot/latest' } })
    const body = await snapshotBody()
    expect(body.approval_request).toBeNull()
    expect(readKeys()).toEqual(['get snapshot/latest.json'])
  })

  it('404s before the first publish', async () => {
    const empty = new FakeR2Bucket()
    const res = await call(accessEnv(empty.binding), '/api/snapshot', {
      token: await mint(signer, { email: 'viewer@example.com' }),
    })
    expect(res.status).toBe(404)
    expectApiJson(res)
    expect(await res.json()).toEqual({ error: 'no snapshot has been published yet' })
  })

  it('502s on a corrupt snapshot without echoing it', async () => {
    bucket.seed('snapshot/latest.json', '{"schema":1, "as_of": TRUNCATED')
    const res = await authed('/api/snapshot')
    expect(res.status).toBe(502)
    expect(await res.json()).toEqual({ error: 'the published snapshot is not valid JSON' })
    bucket.seed('snapshot/latest.json', '[1,2,3]')
    expect((await authed('/api/snapshot')).status).toBe(502)
  })

  it('answers HEAD without a body and refuses other methods', async () => {
    const head = await authed('/api/snapshot', { method: 'HEAD' })
    expect(head.status).toBe(200)
    expect(await head.text()).toBe('')
    for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
      const res = await authed('/api/snapshot', { method, body: method === 'DELETE' ? undefined : '{}' })
      expect(res.status).toBe(405)
      expect(res.headers.get('Allow')).toBe('GET, HEAD')
      expectApiJson(res)
    }
  })
})

describe('GET /api/chart', () => {
  it('returns chart/<KEY>.json unchanged', async () => {
    const res = await authed('/api/chart?symbol=ACME')
    expect(res.status).toBe(200)
    expectApiJson(res)
    expect(await res.json()).toEqual(acmeJson)
    expect(readKeys()).toEqual(['get chart/ACME.json'])
  })

  it('maps a crypto symbol to its "-" key, encoded or not', async () => {
    for (const q of ['QBIT%2FUSD', 'QBIT/USD', 'QBIT-USD']) {
      bucket.calls.length = 0
      const res = await authed(`/api/chart?symbol=${q}`)
      expect(res.status).toBe(200)
      expect(Object.keys((await res.json<ChartData>()).symbols)).toEqual(['QBIT/USD'])
      expect(readKeys()).toEqual(['get chart/QBIT-USD.json'])
    }
  })

  it('404s a symbol with no chart yet', async () => {
    const res = await authed('/api/chart?symbol=ORBT')
    expect(res.status).toBe(404)
    expect(await res.json()).toEqual({ error: 'no chart has been published for this symbol' })
  })

  it('400s anything that is not one trading symbol, without touching R2', async () => {
    for (const q of [
      '',
      'symbol=',
      'symbol=acme',
      'symbol=..%2Fsnapshot%2Flatest',
      'symbol=../snapshot/latest',
      'symbol=ACME%00',
      'symbol=ACME.json',
      'symbol=A/B/C',
      'symbol=%2FACME',
      'symbol=ACME&symbol=NOVA',
      `symbol=${'A'.repeat(16)}`,
      'symbol=AC%20ME',
    ]) {
      const res = await authed(`/api/chart?${q}`)
      expect(res.status, q).toBe(400)
      expectApiJson(res)
    }
    expect(bucket.calls).toEqual([])
  })
})

describe('GET /api/look/asset', () => {
  it('serves the plate as image/png with revalidation headers', async () => {
    const res = await authed(`/api/look/asset?date=${LOOK_DATE}&kind=plate`)
    expect(res.status).toBe(200)
    expectSecurityHeaders(res)
    expect(res.headers.get('Content-Type')).toBe('image/png')
    expect(res.headers.get('Cache-Control')).toBe('private, no-cache')
    expect(res.headers.get('ETag')).toMatch(/^"[0-9a-f]+"$/)
    expect(res.headers.get('Content-Length')).toBe(String(PNG.byteLength))
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PNG)
    expect(readKeys()).toEqual([`get look/${LOOK_DATE}/plate.png`])
  })

  it('forces image/png whatever Content-Type the object was stored with', async () => {
    const res = await authed(`/api/look/asset?date=${LOOK_DATE}&kind=keyframe`)
    expect(res.status).toBe(200)
    expect(res.headers.get('Content-Type')).toBe('image/png')
  })

  it('answers 304 to a matching If-None-Match', async () => {
    const first = await authed(`/api/look/asset?date=${LOOK_DATE}&kind=plate`)
    const etag = first.headers.get('ETag') ?? ''
    const res = await authed(`/api/look/asset?date=${LOOK_DATE}&kind=plate`, { headers: { 'If-None-Match': etag } })
    expect(res.status).toBe(304)
    expect(await res.text()).toBe('')
    expect(res.headers.get('ETag')).toBe(etag)
  })

  it('serves the versioned URL the snapshot carries from the same key', async () => {
    // The publisher appends &v=<content hash> so a redraw is a new URL; the
    // key is still look/<date>/<kind>.png and revalidation is unchanged.
    const res = await authed(`/api/look/asset?date=${LOOK_DATE}&kind=plate&v=0123456789ab`)
    expect(res.status).toBe(200)
    expect(res.headers.get('Cache-Control')).toBe('private, no-cache')
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PNG)
    expect(readKeys()).toEqual([`get look/${LOOK_DATE}/plate.png`])
  })

  it('404s a look image that was not uploaded', async () => {
    const res = await authed('/api/look/asset?date=2026-03-03&kind=plate')
    expect(res.status).toBe(404)
    expectApiJson(res)
  })

  it('400s a bad date or kind without touching R2', async () => {
    for (const q of [
      'date=2026-3-4&kind=plate',
      'date=2026-02-30&kind=plate',
      `date=${LOOK_DATE}&kind=avatar`,
      `date=${LOOK_DATE}&kind=PLATE`,
      `date=${LOOK_DATE}`,
      'kind=plate',
      'date=..%2F..%2Fsnapshot&kind=plate',
      `date=${LOOK_DATE}%2F..&kind=plate`,
      `date=${LOOK_DATE}&kind=plate%2F..%2F..%2Fsnapshot%2Flatest.json`,
      `date=${LOOK_DATE}&date=2026-03-05&kind=plate`,
    ]) {
      const res = await authed(`/api/look/asset?${q}`)
      expect(res.status, q).toBe(400)
    }
    expect(bucket.calls).toEqual([])
  })
})

describe('routing and hardening', () => {
  it('404s unknown API routes (after auth) and anything outside /api and /media (before auth)', async () => {
    for (const path of ['/api/nope', '/api/snapshot/', '/api/look', '/api/look/approve/x']) {
      const res = await authed(path)
      expect(res.status, path).toBe(404)
      expectApiJson(res)
    }
    for (const path of ['/', '/index.html', '/assets/app.js', '/apix/snapshot', '/snapshot/latest.json']) {
      const res = await call(env, path)
      expect(res.status, path).toBe(404)
      expectApiJson(res)
    }
    expect(bucket.calls).toEqual([])
  })

  it('keeps a dot-segment path from reaching other R2 keys', async () => {
    // The URL parser resolves these before the Worker sees them; they land on
    // paths the Worker 404s, so no R2 read happens at all.
    for (const path of ['/media/../snapshot/latest.json', '/media/%2e%2e/snapshot/latest.json', '/media/.%2E/approvals/x.json']) {
      const res = await authed(path)
      expect(res.status, path).toBe(404)
    }
    expect(bucket.calls).toEqual([])
  })

  it('puts the security headers on every kind of response', async () => {
    const unauth = await call(env, '/api/snapshot')
    const unconfigured = await call(accessEnv(bucket.binding, { ACCESS_AUD: '' }), '/api/snapshot')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    for (const res of [
      await authed('/api/snapshot'),
      await authed('/api/chart?symbol=ACME'),
      await authed('/api/nope'),
      unauth,
      unconfigured,
    ]) {
      expectApiJson(res)
    }
    expectSecurityHeaders(await authed(`/api/look/asset?date=${LOOK_DATE}&kind=plate`))
  })

  it('turns an unexpected failure into a generic 500 and logs no detail', async () => {
    const secret = 'r2-detail-that-must-not-leak'
    bucket.binding.get = async () => {
      throw new Error(secret)
    }
    const errors: string[] = []
    vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      errors.push(args.map(String).join(' '))
    })
    const res = await authed('/api/snapshot')
    expect(res.status).toBe(500)
    expectApiJson(res)
    const text = await res.text()
    expect(JSON.parse(text)).toEqual({ error: 'internal error' })
    expect(text).not.toContain(secret)
    expect(errors).toHaveLength(1)
    expect(errors[0]).not.toContain(secret)
  })
})
