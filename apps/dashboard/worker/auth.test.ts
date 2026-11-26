import { SignJWT, base64url } from 'jose'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import snapshotJson from '../fixtures/snapshot.json' with { type: 'json' }
import type { ApproveResponse } from '../src/contract.ts'
import { DEV_BYPASS_IDENTITY, readAccessConfig } from './auth.ts'
import { isLoopbackAddress, sameKey } from './devPeer.ts'
import { accessEnv, AUD, makeSigner, mint, serveCerts, TEAM_DOMAIN, type Signer } from './test/access.ts'
import { call, DEV_KEY, LOCAL_ORIGIN, LOCAL_PEER } from './test/call.ts'
import { FakeR2Bucket } from './test/fake-r2.ts'

let signer: Signer
let stranger: Signer // a key pair Access never published
let bucket: FakeR2Bucket
let logs: string[]

beforeAll(async () => {
  signer = await makeSigner('access-key-1')
  stranger = await makeSigner('stranger-key')
})

beforeEach(() => {
  bucket = new FakeR2Bucket()
  bucket.seed('snapshot/latest.json', snapshotJson, 'application/json')
  serveCerts({ [TEAM_DOMAIN]: { keys: [signer.jwk] } })
  logs = []
  for (const level of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
      logs.push(args.map(String).join(' '))
    })
  }
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

afterAll(() => {
  vi.useRealTimers()
})

/**
 * Read the body, checking that neither it, nor a header, nor anything logged
 * repeats the token or a recognisable segment of it. Returns the parsed JSON.
 */
async function bodyWithoutLeak(res: Response, token: string): Promise<unknown> {
  const text = await res.text()
  const parts = [token, ...token.split('.').filter((p) => p.length >= 8)]
  for (const part of parts) {
    expect(text).not.toContain(part)
    for (const [, value] of res.headers) expect(value).not.toContain(part)
    for (const line of logs) expect(line).not.toContain(part)
  }
  return JSON.parse(text)
}

describe('valid Access token', () => {
  it('serves the request', async () => {
    const token = await mint(signer, { email: 'viewer@example.com' })
    const res = await call(accessEnv(bucket.binding), '/api/snapshot', { token })
    expect(res.status).toBe(200)
    expect(res.headers.get('X-Dev-Auth-Bypass')).toBeNull()
    await bodyWithoutLeak(res, token)
  })

  it('accepts a service token (common_name, no email)', async () => {
    const token = await mint(signer, { common_name: 'svc-client.access' })
    const res = await call(accessEnv(bucket.binding), '/api/snapshot', { token })
    expect(res.status).toBe(200)
  })

  it('accepts a single-string aud and a team domain written without scheme or with a slash', async () => {
    const token = await mint(signer, { email: 'viewer@example.com' }, { aud: AUD })
    const bare = TEAM_DOMAIN.replace(/^https:\/\//, '')
    for (const domain of [bare, `${TEAM_DOMAIN}/`, `  ${TEAM_DOMAIN}  `]) {
      const res = await call(accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: domain }), '/api/snapshot', { token })
      expect(res.status).toBe(200)
    }
  })

  it('protects media and unknown routes too', async () => {
    for (const path of ['/media/history.json', '/api/does-not-exist', '/media/2026-03-04/video/x.mp4']) {
      const res = await call(accessEnv(bucket.binding), path)
      expect(res.status).toBe(401)
    }
    expect(bucket.calls).toEqual([])
  })
})

describe('rejected tokens (401, nothing read from R2, token never echoed)', () => {
  const cases: Array<[name: string, code: string, token: () => Promise<string>]> = [
    ['expired', 'ERR_JWT_EXPIRED', () => mint(signer, { email: 'a@example.com' }, { expIn: -600 })],
    ['not yet valid', 'ERR_JWT_CLAIM_VALIDATION_FAILED', () => mint(signer, { email: 'a@example.com' }, { nbfIn: 600 })],
    ['wrong audience', 'ERR_JWT_CLAIM_VALIDATION_FAILED', () => mint(signer, { email: 'a@example.com' }, { aud: 'f'.repeat(64) })],
    ['wrong issuer', 'ERR_JWT_CLAIM_VALIDATION_FAILED', () => mint(signer, { email: 'a@example.com' }, { iss: 'https://other-team.cloudflareaccess.example' })],
    ['no exp claim', 'ERR_JWT_CLAIM_VALIDATION_FAILED', () => mint(signer, { email: 'a@example.com' }, { expIn: null })],
    ['signed by a key Access never published', 'ERR_JWKS_NO_MATCHING_KEY', () => mint(stranger, { email: 'a@example.com' })],
    [
      'published kid but the wrong key',
      'ERR_JWS_SIGNATURE_VERIFICATION_FAILED',
      () => mint(stranger, { email: 'a@example.com' }, { kid: 'access-key-1' }),
    ],
    [
      'HS256 instead of RS256',
      'ERR_JOSE_ALG_NOT_ALLOWED',
      () =>
        new SignJWT({ email: 'a@example.com' })
          .setProtectedHeader({ alg: 'HS256', kid: 'access-key-1' })
          .setIssuer(TEAM_DOMAIN)
          .setAudience(AUD)
          .setExpirationTime('10m')
          .sign(crypto.getRandomValues(new Uint8Array(32))),
    ],
    [
      'alg none',
      'ERR_JOSE_ALG_NOT_ALLOWED',
      async () => {
        const enc = (v: object) => base64url.encode(JSON.stringify(v))
        const now = Math.floor(Date.now() / 1000)
        return `${enc({ alg: 'none', typ: 'JWT' })}.${enc({ iss: TEAM_DOMAIN, aud: AUD, exp: now + 600, email: 'a@example.com' })}.`
      },
    ],
    [
      'tampered payload',
      'ERR_JWS_SIGNATURE_VERIFICATION_FAILED',
      async () => {
        const token = await mint(signer, { email: 'a@example.com' })
        const [h, , s] = token.split('.')
        const forged = base64url.encode(
          JSON.stringify({ iss: TEAM_DOMAIN, aud: [AUD], exp: 4_000_000_000, email: 'admin@example.com' }),
        )
        return `${h}.${forged}.${s}`
      },
    ],
    ['garbage', 'ERR_JWS_INVALID', async () => 'not-a-jwt'],
    ['three empty segments', 'ERR_JWS_INVALID', async () => '..'],
  ]

  for (const [name, code, makeToken] of cases) {
    it(`${name} (${code})`, async () => {
      const token = await makeToken()
      const res = await call(accessEnv(bucket.binding), '/api/snapshot', { token })
      expect(res.status).toBe(401)
      expect(res.headers.get('Cache-Control')).toBe('no-store')
      expect(await bodyWithoutLeak(res, token)).toEqual({ error: 'invalid Access token' })
      expect(logs).toEqual([`access: token rejected (${code})`])
      expect(bucket.calls).toEqual([])
    })
  }

  it('missing header', async () => {
    const res = await call(accessEnv(bucket.binding), '/api/snapshot')
    expect(res.status).toBe(401)
    expect(await res.json()).toEqual({ error: 'missing Access token' })
    expect(bucket.calls).toEqual([])
  })

  it('an oversized header is refused without being parsed', async () => {
    const token = 'a'.repeat(20_000)
    const res = await call(accessEnv(bucket.binding), '/api/snapshot', { token })
    expect(res.status).toBe(401)
    await bodyWithoutLeak(res, token)
  })

  it('a token in a cookie or Authorization header is not accepted', async () => {
    const token = await mint(signer, { email: 'a@example.com' })
    const res = await call(accessEnv(bucket.binding), '/api/snapshot', {
      headers: { Cookie: `CF_Authorization=${token}`, Authorization: `Bearer ${token}` },
    })
    expect(res.status).toBe(401)
  })
})

describe('JWKS handling', () => {
  it('fetches the team certs once and reuses them across requests', async () => {
    const domain = 'https://cache-check.cloudflareaccess.example'
    const certs = serveCerts({ [domain]: { keys: [signer.jwk] } })
    const env = accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: domain })
    for (let i = 0; i < 5; i++) {
      const token = await mint(signer, { email: 'a@example.com' }, { iss: domain })
      expect((await call(env, '/api/snapshot', { token })).status).toBe(200)
    }
    expect(certs.hits(`${domain}/cdn-cgi/access/certs`)).toBe(1)
  })

  it('refuses (503, fail closed) when the certs cannot be fetched', async () => {
    const domain = 'https://certs-down.cloudflareaccess.example'
    serveCerts({ [domain]: { status: 500 } })
    const token = await mint(signer, { email: 'a@example.com' }, { iss: domain })
    const res = await call(accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: domain }), '/api/snapshot', { token })
    expect(res.status).toBe(503)
    expect(await bodyWithoutLeak(res, token)).toEqual({ error: 'cannot verify identity right now' })
    expect(bucket.calls).toEqual([])
  })

  it('refuses (503) when the certs fetch throws', async () => {
    const domain = 'https://certs-unreachable.cloudflareaccess.example'
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network down')
      }),
    )
    const token = await mint(signer, { email: 'a@example.com' }, { iss: domain })
    const res = await call(accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: domain }), '/api/snapshot', { token })
    expect(res.status).toBe(503)
  })
})

describe('fail closed when Access is not configured', () => {
  const unconfigured: Array<[string, Partial<Env>]> = [
    ['both empty', { ACCESS_TEAM_DOMAIN: '', ACCESS_AUD: '' }],
    ['team domain empty', { ACCESS_TEAM_DOMAIN: '' }],
    ['aud empty', { ACCESS_AUD: '' }],
    ['whitespace only', { ACCESS_TEAM_DOMAIN: '   ', ACCESS_AUD: ' ' }],
    ['undefined', { ACCESS_TEAM_DOMAIN: undefined, ACCESS_AUD: undefined } as unknown as Partial<Env>],
  ]

  for (const [name, overrides] of unconfigured) {
    it(`refuses every /api and /media request: ${name}`, async () => {
      // Even a token that WOULD verify does not get in.
      const token = await mint(signer, { email: 'a@example.com' })
      const env = accessEnv(bucket.binding, overrides)
      for (const path of ['/api/snapshot', '/api/chart?symbol=ACME', '/media/history.json']) {
        const res = await call(env, path, { token })
        expect(res.status).toBe(503)
        expect(await res.json()).toEqual({ error: 'authentication is not configured' })
      }
      const post = await call(env, '/api/look/approve', { method: 'POST', token, body: '{}' })
      expect(post.status).toBe(503)
      expect(bucket.calls).toEqual([])
    })
  }

  const invalidDomains = [
    'http://team.cloudflareaccess.com',
    'https://team.cloudflareaccess.com/cdn-cgi/access/certs',
    'https://team.cloudflareaccess.com?x=1',
    'https://user:pw@team.cloudflareaccess.com',
    'https://team.cloudflareaccess.com:8443',
    'ftp://team.cloudflareaccess.com',
    'https://',
  ]
  for (const domain of invalidDomains) {
    it(`treats a malformed team domain as misconfigured, bypass or not: ${domain}`, async () => {
      expect(readAccessConfig({ ACCESS_TEAM_DOMAIN: domain, ACCESS_AUD: AUD }).state).toBe('invalid')
      const env = accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: domain, DEV_AUTH_BYPASS: '1' })
      const res = await call(env, '/api/snapshot', { origin: LOCAL_ORIGIN })
      expect(res.status).toBe(503)
    })
  }
})

describe('DEV_AUTH_BYPASS', () => {
  // What `vite dev` gives the Worker: .dev.vars plus the dev server's key.
  const devEnv = (bypass: string | undefined, key: string | undefined = DEV_KEY) =>
    accessEnv(bucket.binding, { ACCESS_TEAM_DOMAIN: '', ACCESS_AUD: '', DEV_AUTH_BYPASS: bypass, DEV_PEER_KEY: key })

  it('opens the API to a loopback peer on a loopback host when Access is unset and it is exactly "1"', async () => {
    for (const origin of [LOCAL_ORIGIN, 'http://127.0.0.1:8788', 'http://[::1]:5173', 'http://dash.localhost:5173']) {
      for (const peer of [LOCAL_PEER, '127.0.0.2', '::1', '::ffff:127.0.0.1']) {
        const res = await call(devEnv('1'), '/api/snapshot', { origin, peer })
        expect(res.status).toBe(200)
        expect(res.headers.get('X-Dev-Auth-Bypass')).toBe('1')
      }
    }
  })

  it('records the bypass identity on an approval', async () => {
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date())
    const res = await call(devEnv('1'), '/api/look/approve', {
      origin: LOCAL_ORIGIN,
      peer: LOCAL_PEER,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'alpaca-dashboard' },
      body: JSON.stringify({ date: today, approved: true }),
    })
    expect(res.status).toBe(202)
    expect((await res.json<ApproveResponse>()).request.requested_by).toBe(DEV_BYPASS_IDENTITY)
  })

  for (const value of ['true', 'yes', 'on', '01', ' 1', '1 ', 'TRUE', '', undefined]) {
    it(`does nothing for DEV_AUTH_BYPASS=${JSON.stringify(value)}`, async () => {
      const res = await call(devEnv(value), '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER })
      expect(res.status).toBe(503)
    })
  }

  it('never applies on a non-loopback host, even when set', async () => {
    for (const origin of ['https://alpaca-dashboard.unit-test.workers.dev', 'http://192.168.1.20:5173', 'http://localhost.example.com']) {
      const res = await call(devEnv('1'), '/api/snapshot', { origin, peer: LOCAL_PEER })
      expect(res.status).toBe(503)
    }
  })

  // SEC-1: a dev server started with --host builds request.url from the
  // client's Host header, so "Host: localhost" from a phone on the LAN looked
  // local. Only the peer address the Vite server stamps can tell.
  it('never applies to a request from a network peer, whatever Host it sends', async () => {
    for (const peer of ['192.168.1.20', '10.0.0.7', '172.17.0.1', '::ffff:192.168.1.20', 'fe80::1', '128.0.0.1', '1.2.3.4']) {
      const res = await call(devEnv('1'), '/api/snapshot', { origin: LOCAL_ORIGIN, peer })
      expect(res.status).toBe(503)
      expect(res.headers.get('X-Dev-Auth-Bypass')).toBeNull()
    }
    const media = await call(devEnv('1'), '/media/history.json', { origin: 'http://foo.localhost:5199', peer: '192.168.1.20' })
    expect(media.status).toBe(503)
  })

  it('never applies without the stamped peer', async () => {
    for (const peer of [undefined, '', 'localhost', '127.0.0.1, 192.168.1.20', '127.0.0.256', 'not an address']) {
      const res = await call(devEnv('1'), '/api/snapshot', { origin: LOCAL_ORIGIN, peer, peerKey: DEV_KEY })
      expect(res.status).toBe(503)
    }
  })

  // `wrangler dev` reads the same .dev.vars but runs no Vite middleware, so a
  // client could send both headers itself. Only `vite dev` has the key.
  it('never applies without the dev server key, whatever headers arrive', async () => {
    for (const peerKey of [null, '', 'guessed', DEV_KEY.slice(1), `${DEV_KEY}x`, DEV_KEY.toUpperCase()]) {
      const res = await call(devEnv('1'), '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER, peerKey })
      expect(res.status, String(peerKey)).toBe(503)
    }
    for (const key of [undefined, '', 'short-key']) {
      // wrangler dev / vite preview: no key in the env (or not one vite dev made)
      const res = await call(devEnv('1', key), '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER, peerKey: key ?? '' })
      expect(res.status, String(key)).toBe(503)
    }
  })

  it('never applies to a request relayed by a proxy or tunnel on this machine', async () => {
    const relayed: Record<string, string>[] = [
      { 'X-Forwarded-For': '203.0.113.9' },
      { Forwarded: 'for=203.0.113.9' },
      { 'X-Real-IP': '203.0.113.9' },
      { 'True-Client-IP': '203.0.113.9' },
      { 'CF-Connecting-IP': '203.0.113.9' },
    ]
    for (const headers of relayed) {
      const res = await call(devEnv('1'), '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER, headers })
      expect(res.status).toBe(503)
    }
    // workerd itself fills CF-Connecting-IP with the loopback hop under vite dev.
    const local = await call(devEnv('1'), '/api/snapshot', {
      origin: LOCAL_ORIGIN,
      peer: LOCAL_PEER,
      headers: { 'CF-Connecting-IP': '127.0.0.1' },
    })
    expect(local.status).toBe(200)
  })

  it('never applies on the deployed hostname, even with a forged peer header', async () => {
    const res = await call(devEnv('1'), '/api/snapshot', {
      peer: LOCAL_PEER,
      headers: { 'CF-Connecting-IP': '198.51.100.4' },
    })
    expect(res.status).toBe(503)
  })

  it('is ignored once Access is configured: the JWT is still required', async () => {
    const env = accessEnv(bucket.binding, { DEV_AUTH_BYPASS: '1' })
    const anonymous = await call(env, '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER })
    expect(anonymous.status).toBe(401)
    const token = await mint(signer, { email: 'a@example.com' })
    const authed = await call(env, '/api/snapshot', { origin: LOCAL_ORIGIN, peer: LOCAL_PEER, token })
    expect(authed.status).toBe(200)
    expect(authed.headers.get('X-Dev-Auth-Bypass')).toBeNull()
  })
})

describe('sameKey', () => {
  it('is plain equality, nothing looser', () => {
    expect(sameKey(DEV_KEY, DEV_KEY)).toBe(true)
    for (const other of [null, undefined, '', DEV_KEY.slice(1), `${DEV_KEY} `, DEV_KEY.toUpperCase()]) {
      expect(sameKey(DEV_KEY, other)).toBe(false)
      expect(sameKey(other, DEV_KEY)).toBe(false)
    }
  })
})

describe('isLoopbackAddress', () => {
  it('accepts 127.0.0.0/8, ::1 and IPv4-mapped 127.x', () => {
    for (const a of ['127.0.0.1', '127.1.2.3', '127.255.255.255', '::1', '0:0:0:0:0:0:0:1', '::ffff:127.0.0.1', ' 127.0.0.1 ']) {
      expect(isLoopbackAddress(a)).toBe(true)
    }
  })

  it('refuses everything else', () => {
    for (const a of [null, undefined, '', 'localhost', '0.0.0.0', '126.0.0.1', '127.0.0', '127.0.0.1.5', '127.0.0.256',
      '::', '::2', '::ffff:10.0.0.1', '192.168.1.20', '127.0.0.1,127.0.0.1', '[::1]']) {
      expect(isLoopbackAddress(a)).toBe(false)
    }
  })
})
