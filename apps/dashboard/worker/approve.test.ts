// POST /api/look/approve: the Worker's one write.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { CSRF_HEADER, CSRF_VALUE, type ApprovalRequest, type ApproveResponse } from '../src/contract.ts'
import { approvalWindow, easternDate } from './dates.ts'
import { accessEnv, makeSigner, mint, serveCerts, TEAM_DOMAIN, type Signer } from './test/access.ts'
import { call, ORIGIN, type CallInit } from './test/call.ts'
import { FakeR2Bucket } from './test/fake-r2.ts'

// 2026-03-05 03:30 UTC is still 2026-03-04 (22:30 EST) in New York, so the
// window is 03-03..03-07 in ET, where a UTC-based one would be 03-04..03-08.
const NOW = new Date('2026-03-05T03:30:00Z')
const ET_TODAY = '2026-03-04'

let signer: Signer
let bucket: FakeR2Bucket
let env: Env

beforeAll(async () => {
  signer = await makeSigner('approve-key')
})

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  serveCerts({ [TEAM_DOMAIN]: { keys: [signer.jwk] } })
  bucket = new FakeR2Bucket()
  env = accessEnv(bucket.binding)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const GOOD_HEADERS = { 'Content-Type': 'application/json', [CSRF_HEADER]: CSRF_VALUE }

interface ApproveInit extends Omit<CallInit, 'body'> {
  body?: unknown
  claims?: Record<string, unknown>
  rawBody?: BodyInit
}

async function approve(init: ApproveInit = {}): Promise<Response> {
  const { body = { date: ET_TODAY, approved: true }, claims = { email: 'reviewer@example.com' }, rawBody, ...rest } = init
  return call(env, '/api/look/approve', {
    method: 'POST',
    headers: GOOD_HEADERS,
    body: rawBody ?? JSON.stringify(body),
    token: await mint(signer, claims),
    ...rest,
  })
}

const puts = () => bucket.calls.filter((c) => c.op === 'put')

async function expectRefused(res: Response, status: number, error?: string): Promise<void> {
  expect(res.status).toBe(status)
  expect(res.headers.get('Cache-Control')).toBe('no-store')
  const body = await res.json<{ error: unknown }>()
  if (error !== undefined) expect(body).toEqual({ error })
  else expect(typeof body.error).toBe('string')
  expect(puts()).toEqual([])
}

describe('accepted', () => {
  it('writes approvals/<date>.json and returns it (202)', async () => {
    const res = await approve()
    expect(res.status).toBe(202)
    expect(res.headers.get('Cache-Control')).toBe('no-store')
    const expected: ApprovalRequest = {
      date: ET_TODAY,
      approved: true,
      requested_at: '2026-03-05T03:30:00+00:00',
      requested_by: 'reviewer@example.com',
    }
    expect(await res.json()).toEqual({ request: expected })
    expect(bucket.keys()).toEqual([`approvals/${ET_TODAY}.json`])
    expect(JSON.parse(bucket.text(`approvals/${ET_TODAY}.json`) ?? '')).toEqual(expected)
    expect(bucket.stored(`approvals/${ET_TODAY}.json`)?.httpMetadata.contentType).toBe('application/json')
  })

  it('records a revoke (approved: false) and overwrites the earlier request', async () => {
    await approve()
    const res = await approve({ body: { date: ET_TODAY, approved: false } })
    expect(res.status).toBe(202)
    expect(JSON.parse(bucket.text(`approvals/${ET_TODAY}.json`) ?? '').approved).toBe(false)
  })

  it('takes requested_by from a service token common_name', async () => {
    const res = await approve({ claims: { common_name: 'svc-client.access' } })
    expect(res.status).toBe(202)
    expect((await res.json<ApproveResponse>()).request.requested_by).toBe('svc-client.access')
  })

  it('prefers email over common_name', async () => {
    const res = await approve({ claims: { email: 'person@example.com', common_name: 'svc' } })
    expect((await res.json<ApproveResponse>()).request.requested_by).toBe('person@example.com')
  })

  it('accepts a charset parameter and a same-origin Origin / Sec-Fetch-Site', async () => {
    const res = await approve({
      headers: {
        ...GOOD_HEADERS,
        'Content-Type': 'Application/JSON; charset=utf-8',
        Origin: ORIGIN,
        'Sec-Fetch-Site': 'same-origin',
      },
    })
    expect(res.status).toBe(202)
  })

  it('accepts the window edges in US/Eastern: today -1 and today +3', async () => {
    expect(easternDate(NOW)).toBe(ET_TODAY)
    expect(approvalWindow(NOW)).toEqual({ earliest: '2026-03-03', latest: '2026-03-07' })
    for (const date of ['2026-03-03', '2026-03-04', '2026-03-05', '2026-03-07']) {
      const res = await approve({ body: { date, approved: true } })
      expect(res.status, date).toBe(202)
    }
  })
})

describe('refused, and nothing is written', () => {
  it('outside the window (422), judged in US/Eastern not UTC', async () => {
    // 2026-03-08 would be inside a UTC window (UTC today is already 03-05).
    for (const date of ['2026-03-02', '2026-03-08', '2025-03-04', '2027-03-04']) {
      await expectRefused(
        await approve({ body: { date, approved: true } }),
        422,
        'date is outside the approval window (US/Eastern today -1 to +3 days)',
      )
    }
  })

  it('without the CSRF header, or with the wrong value (403)', async () => {
    await expectRefused(
      await approve({ headers: { 'Content-Type': 'application/json' } }),
      403,
      'missing X-Requested-With header',
    )
    for (const value of ['XMLHttpRequest', 'ALPACA-DASHBOARD', 'alpaca-dashboard-x', '']) {
      await expectRefused(await approve({ headers: { ...GOOD_HEADERS, [CSRF_HEADER]: value } }), 403)
    }
  })

  it('from another origin or site (403)', async () => {
    await expectRefused(
      await approve({ headers: { ...GOOD_HEADERS, Origin: 'https://evil.example' } }),
      403,
      'cross-origin request refused',
    )
    await expectRefused(await approve({ headers: { ...GOOD_HEADERS, Origin: 'null' } }), 403)
    for (const site of ['cross-site', 'same-site', 'none']) {
      await expectRefused(await approve({ headers: { ...GOOD_HEADERS, 'Sec-Fetch-Site': site } }), 403)
    }
  })

  it('without a JSON Content-Type (415)', async () => {
    for (const type of ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x', 'application/jsonx', 'application/json-patch+json']) {
      await expectRefused(
        await approve({ headers: { [CSRF_HEADER]: CSRF_VALUE, 'Content-Type': type } }),
        415,
        'Content-Type must be application/json',
      )
    }
    await expectRefused(
      await approve({ headers: { [CSRF_HEADER]: CSRF_VALUE }, rawBody: new Uint8Array([123, 125]) }),
      415,
    )
  })

  it('with a malformed body (400)', async () => {
    const bodies: unknown[] = [
      [],
      'yes',
      null,
      {},
      { date: ET_TODAY },
      { approved: true },
      { date: ET_TODAY, approved: 'true' },
      { date: ET_TODAY, approved: 1 },
      { date: ET_TODAY, approved: null },
      { date: 20260304, approved: true },
      { date: '2026-3-4', approved: true },
      { date: '2026-03-04T00:00:00Z', approved: true },
      { date: '2026-02-30', approved: true },
      { date: '2026-03-04 ', approved: true },
      { date: '../2026-03-04', approved: true },
      { date: ET_TODAY, approved: true, requested_by: 'someone-else@example.com' },
    ]
    for (const body of bodies) {
      await expectRefused(await approve({ body }), 400)
    }
    for (const raw of [
      '',
      '{',
      '{"date": "2026-03-04", "approved": true',
      'true',
      '{"date": "2026-03-04", "approved": true, "__proto__": {"approved": false}}',
    ]) {
      await expectRefused(await approve({ rawBody: raw }), 400)
    }
  })

  it('with a body that is not UTF-8 (400)', async () => {
    await expectRefused(await approve({ rawBody: new Uint8Array([0x7b, 0xff, 0xfe, 0x7d]) }), 400, 'body is not valid UTF-8')
  })

  it('with an oversized body, declared or streamed (413)', async () => {
    const padding = ' '.repeat(4096)
    await expectRefused(
      await approve({ rawBody: `{"date":"${ET_TODAY}","approved":true}${padding}` }),
      413,
      'body too large',
    )
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < 8; i++) controller.enqueue(new TextEncoder().encode(padding))
        controller.close()
      },
    })
    await expectRefused(
      await approve({ rawBody: stream, duplex: 'half' } as ApproveInit & { duplex: 'half' }),
      413,
    )
  })

  it('when the token carries no identity (403)', async () => {
    await expectRefused(
      await approve({ claims: {} }),
      403,
      'the Access token carries no email or service-token name',
    )
    await expectRefused(await approve({ claims: { email: '' } }), 403)
    await expectRefused(await approve({ claims: { email: 42 } }), 403)
  })

  it('without a valid token (401)', async () => {
    const res = await call(env, '/api/look/approve', {
      method: 'POST',
      headers: GOOD_HEADERS,
      body: JSON.stringify({ date: ET_TODAY, approved: true }),
    })
    await expectRefused(res, 401)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const expired = await approve({ token: await mint(signer, { email: 'a@example.com' }, { expIn: -120 }) })
    await expectRefused(expired, 401)
  })

  it('for any method but POST (405)', async () => {
    for (const method of ['GET', 'HEAD', 'PUT', 'DELETE', 'PATCH']) {
      const res = await call(env, '/api/look/approve', {
        method,
        headers: GOOD_HEADERS,
        token: await mint(signer, { email: 'a@example.com' }),
        body: method === 'GET' || method === 'HEAD' ? undefined : '{}',
      })
      expect(res.status, method).toBe(405)
      expect(res.headers.get('Allow')).toBe('POST')
    }
    expect(puts()).toEqual([])
  })
})

describe('the US/Eastern window across a DST change', () => {
  it('uses the ET calendar date on both sides of the spring-forward night', async () => {
    // 2026-03-08 is the US spring-forward Sunday.
    vi.setSystemTime(new Date('2026-03-09T03:59:00Z')) // 23:59 EDT on 03-08
    expect(easternDate(new Date())).toBe('2026-03-08')
    vi.setSystemTime(new Date('2026-03-09T04:00:00Z')) // 00:00 EDT on 03-09
    expect(easternDate(new Date())).toBe('2026-03-09')
    expect((await approve({ body: { date: '2026-03-12', approved: true } })).status).toBe(202)
    expect((await approve({ body: { date: '2026-03-13', approved: true } })).status).toBe(422)
  })
})
