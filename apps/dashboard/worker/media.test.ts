// GET|HEAD /media/<path>: path validation, Range (206/416), revalidation (304).
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import historyJson from '../fixtures/history.json' with { type: 'json' }
import { accessEnv, makeSigner, mint, serveCerts, TEAM_DOMAIN, type Signer } from './test/access.ts'
import { call, type CallInit } from './test/call.ts'
import { FakeR2Bucket } from './test/fake-r2.ts'

// The fallback edition's mp4: a YouTube edition's video is never in R2.
const VIDEO_PATH = '2026-03-04/slideshow-midday-briefing/midday-briefing_2026-03-04.mp4'
const SIZE = 1000
const VIDEO = Uint8Array.from({ length: SIZE }, (_, i) => i % 251)

let signer: Signer
let bucket: FakeR2Bucket
let env: Env

beforeAll(async () => {
  signer = await makeSigner('media-key')
})

beforeEach(() => {
  serveCerts({ [TEAM_DOMAIN]: { keys: [signer.jwk] } })
  bucket = new FakeR2Bucket()
  // Stored as text/html on purpose: the served type comes from the extension.
  bucket.seed(`media/${VIDEO_PATH}`, VIDEO, 'text/html')
  bucket.seed('media/history.json', historyJson, 'application/json')
  bucket.seed('media/2026-03-04/slideshow-midday-briefing/meta.json', { date: '2026-03-04' }, 'application/json')
  bucket.seed('media/2026-03-04/video/post-market-recap_2026-03-04_cover.png', new Uint8Array([0x89, 0x50]), 'image/png')
  bucket.seed('snapshot/latest.json', { schema: 1 }, 'application/json')
  env = accessEnv(bucket.binding)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function media(path: string, init: CallInit = {}): Promise<Response> {
  return call(env, `/media/${path}`, { ...init, token: await mint(signer, { email: 'viewer@example.com' }) })
}

async function range(header: string, extra: Record<string, string> = {}): Promise<Response> {
  return media(VIDEO_PATH, { headers: { Range: header, ...extra } })
}

async function bytesOf(res: Response): Promise<Uint8Array> {
  return new Uint8Array(await res.arrayBuffer())
}

describe('full responses', () => {
  it('streams a video with its validators, as video/mp4 whatever it was stored as', async () => {
    const res = await media(VIDEO_PATH)
    expect(res.status).toBe(200)
    expect(res.headers.get('Content-Type')).toBe('video/mp4')
    expect(res.headers.get('Content-Length')).toBe(String(SIZE))
    expect(res.headers.get('Accept-Ranges')).toBe('bytes')
    expect(res.headers.get('ETag')).toMatch(/^"[0-9a-f]+"$/)
    expect(res.headers.get('Last-Modified')).toMatch(/GMT$/)
    // Unversioned: the object may be replaced in place by a re-render.
    expect(res.headers.get('Cache-Control')).toBe('private, no-cache')
    expect(res.headers.get('X-Content-Type-Options')).toBe('nosniff')
    expect(res.headers.get('Referrer-Policy')).toBe('no-referrer')
    expect(res.headers.get('Cross-Origin-Resource-Policy')).toBe('same-origin')
    // No CSP on media: default-src 'none' would break the browser's own viewer.
    expect(res.headers.get('Content-Security-Policy')).toBeNull()
    expect(await bytesOf(res)).toEqual(VIDEO)
    expect(bucket.calls.map((c) => `${c.op} ${c.key}`)).toEqual([`get media/${VIDEO_PATH}`])
  })

  // F10: a re-render keeps its file names, so only the content version in
  // history.json's URL tells old bytes from new ones.
  it('lets the browser keep a binary for an hour only under a versioned URL', async () => {
    const versioned = await media(`${VIDEO_PATH}?v=0123456789ab`)
    expect(versioned.status).toBe(200)
    expect(versioned.headers.get('Cache-Control')).toBe('private, max-age=3600')
    expect(await bytesOf(versioned)).toEqual(VIDEO)
    const ranged = await media(`${VIDEO_PATH}?v=0123456789ab`, { headers: { Range: 'bytes=0-9' } })
    expect(ranged.status).toBe(206)
    expect(ranged.headers.get('Cache-Control')).toBe('private, max-age=3600')
    for (const query of ['', '?v=', '?v=XYZ', '?v=0123456789abc', '?v=0123456789ab&v=0123456789ab', '?w=0123456789ab']) {
      const res = await media(`${VIDEO_PATH}${query}`)
      expect(res.status, query).toBe(200)
      expect(res.headers.get('Cache-Control'), query).toBe('private, no-cache')
    }
    const json = await media('history.json?v=0123456789ab')
    expect(json.headers.get('Cache-Control')).toBe('private, no-cache')
  })

  it('serves history.json and meta.json as JSON that always revalidates', async () => {
    const res = await media('history.json')
    expect(res.status).toBe(200)
    expect(res.headers.get('Content-Type')).toBe('application/json; charset=utf-8')
    expect(res.headers.get('Cache-Control')).toBe('private, no-cache')
    expect(res.headers.get('Content-Security-Policy')).toContain("default-src 'none'")
    expect(await res.json()).toEqual(historyJson)
    const meta = await media('2026-03-04/slideshow-midday-briefing/meta.json')
    expect(meta.status).toBe(200)
    expect(await meta.json()).toEqual({ date: '2026-03-04' })
  })

  it('serves a cover as image/png', async () => {
    const res = await media('2026-03-04/video/post-market-recap_2026-03-04_cover.png')
    expect(res.status).toBe(200)
    expect(res.headers.get('Content-Type')).toBe('image/png')
  })

  it('404s a well-formed path that was never uploaded (e.g. a per-scene clip)', async () => {
    const res = await media('2026-03-04/video/clip_00_OVERVIEW.mp4')
    expect(res.status).toBe(404)
    expect(res.headers.get('Content-Type')).toBe('application/json; charset=utf-8')
    expect(await res.json()).toEqual({ error: 'not found' })
  })

  it('answers HEAD from metadata alone', async () => {
    const res = await media(VIDEO_PATH, { method: 'HEAD' })
    expect(res.status).toBe(200)
    expect(res.headers.get('Content-Length')).toBe(String(SIZE))
    expect(res.headers.get('Accept-Ranges')).toBe('bytes')
    expect(await res.text()).toBe('')
    expect(bucket.calls.map((c) => c.op)).toEqual(['head'])
  })

  it('refuses writes', async () => {
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const res = await media(VIDEO_PATH, { method, body: method === 'DELETE' ? undefined : 'x' })
      expect(res.status).toBe(405)
      expect(res.headers.get('Allow')).toBe('GET, HEAD')
    }
    expect(bucket.calls).toEqual([])
  })
})

describe('Range', () => {
  const cases: Array<[header: string, offset: number, length: number]> = [
    ['bytes=0-99', 0, 100],
    ['bytes=100-', 100, SIZE - 100],
    ['bytes=990-5000', 990, 10],
    ['bytes=999-999', 999, 1],
    ['bytes=-50', SIZE - 50, 50],
    ['bytes=-5000', 0, SIZE],
    ['bytes=0-', 0, SIZE],
    ['BYTES = 10 - 19', 10, 10],
  ]
  for (const [header, offset, length] of cases) {
    it(`${header} -> 206 bytes ${offset}-${offset + length - 1}/${SIZE}`, async () => {
      const res = await range(header)
      expect(res.status).toBe(206)
      expect(res.headers.get('Content-Range')).toBe(`bytes ${offset}-${offset + length - 1}/${SIZE}`)
      expect(res.headers.get('Content-Length')).toBe(String(length))
      expect(res.headers.get('Content-Type')).toBe('video/mp4')
      expect(res.headers.get('Accept-Ranges')).toBe('bytes')
      expect(await bytesOf(res)).toEqual(VIDEO.slice(offset, offset + length))
      // head for the size, then exactly those bytes, pinned to that version
      expect(bucket.calls.map((c) => c.op)).toEqual(['head', 'get'])
      const get = bucket.calls[1].options as R2GetOptions
      expect(get.range).toEqual({ offset, length })
      expect(get.onlyIf).toHaveProperty('etagMatches')
    })
  }

  for (const header of [`bytes=${SIZE}-`, `bytes=${SIZE + 5}-${SIZE + 10}`, 'bytes=-0']) {
    it(`${header} -> 416 with Content-Range bytes */${SIZE}`, async () => {
      const res = await range(header)
      expect(res.status).toBe(416)
      expect(res.headers.get('Content-Range')).toBe(`bytes */${SIZE}`)
      expect(await res.json()).toEqual({ error: 'range not satisfiable' })
      expect(bucket.calls.map((c) => c.op)).toEqual(['head'])
    })
  }

  for (const header of ['bytes=0-1,5-6', 'bytes=5-3', 'bytes=abc', 'items=0-5', 'bytes=', 'bytes=-', 'bytes=1-2-3', `bytes=0-${'9'.repeat(20)}`]) {
    it(`ignores ${JSON.stringify(header)} and sends the whole file`, async () => {
      const res = await range(header)
      expect(res.status).toBe(200)
      expect(res.headers.get('Content-Range')).toBeNull()
      expect(await bytesOf(res)).toEqual(VIDEO)
    })
  }

  it('honours If-Range only while the validator still matches', async () => {
    const etag = (await media(VIDEO_PATH)).headers.get('ETag') ?? ''
    const lastModified = (await media(VIDEO_PATH)).headers.get('Last-Modified') ?? ''
    expect((await range('bytes=0-9', { 'If-Range': etag })).status).toBe(206)
    expect((await range('bytes=0-9', { 'If-Range': lastModified })).status).toBe(206)
    for (const stale of ['"0000"', `W/${etag}`, 'Thu, 01 Jan 2026 00:00:00 GMT']) {
      const res = await range('bytes=0-9', { 'If-Range': stale })
      expect(res.status).toBe(200)
      expect(await bytesOf(res)).toEqual(VIDEO)
    }
  })

  it('re-reads when the object is replaced between the size check and the read', async () => {
    const replacement = Uint8Array.from({ length: 600 }, (_, i) => 255 - (i % 200))
    let swapped = false
    bucket.afterHead = (key) => {
      if (!swapped && key === `media/${VIDEO_PATH}`) {
        swapped = true
        bucket.seed(key, replacement, 'video/mp4')
      }
    }
    const res = await range('bytes=500-')
    expect(res.status).toBe(206)
    expect(res.headers.get('Content-Range')).toBe('bytes 500-599/600')
    expect(await bytesOf(res)).toEqual(replacement.slice(500))
    expect(bucket.calls.map((c) => c.op)).toEqual(['head', 'get', 'head', 'get'])
  })
})

describe('revalidation', () => {
  it('304s a matching If-None-Match without reading the body', async () => {
    const etag = (await media(VIDEO_PATH)).headers.get('ETag') ?? ''
    bucket.calls.length = 0
    const res = await media(VIDEO_PATH, { headers: { 'If-None-Match': etag } })
    expect(res.status).toBe(304)
    expect(await res.text()).toBe('')
    expect(res.headers.get('ETag')).toBe(etag)
    expect(res.headers.get('Content-Type')).toBeNull()
    expect(bucket.calls).toHaveLength(1)
    expect((bucket.calls[0].options as R2GetOptions).onlyIf).toEqual({ etagDoesNotMatch: etag.slice(1, -1) })
  })

  it('304s a weak, listed or wildcard match, and a Range request with a match', async () => {
    const etag = (await media(VIDEO_PATH)).headers.get('ETag') ?? ''
    for (const inm of [`W/${etag}`, `"nope", ${etag}`, '*']) {
      expect((await media(VIDEO_PATH, { headers: { 'If-None-Match': inm } })).status, inm).toBe(304)
    }
    expect((await range('bytes=0-9', { 'If-None-Match': etag })).status).toBe(304)
  })

  it('200s a stale If-None-Match', async () => {
    const res = await media(VIDEO_PATH, { headers: { 'If-None-Match': '"stale"' } })
    expect(res.status).toBe(200)
    expect(await bytesOf(res)).toEqual(VIDEO)
  })
})

describe('path validation', () => {
  // Each of these must be refused before any R2 call. The URL parser resolves
  // plain and %2e dot segments first, so those arrive here as other paths and
  // are covered in index.test.ts; what remains must be stopped by the Worker.
  const refused = [
    '..%2Fsnapshot%2Flatest.json',
    '%2e%2e%2fsnapshot%2flatest.json',
    '%2E%2E%2F%2E%2E%2Fsnapshot%2Flatest.json',
    '2026-03-04%2Fvideo%2Fpost-market-recap_2026-03-04.mp4',
    '2026-03-04/video/..%2f..%2f..%2fsnapshot%2flatest.json',
    '2026-03-04/video/%2e%2e',
    '%2Fsnapshot%2Flatest.json',
    '/snapshot/latest.json',
    '//snapshot/latest.json',
    '2026-03-04//video/x.mp4',
    '2026-03-04/video/x.mp4/',
    '2026-03-04/video/.hidden.mp4',
    '2026-03-04/.video/x.mp4',
    '2026-03-04/video/x.mp4%00.png',
    '2026-03-04/video/x%5c..%5csnapshot.json',
    '2026-03-04/video/x.html',
    '2026-03-04/video/x.svg',
    '2026-03-04/video/x.js',
    '2026-03-04/video/x',
    '2026-03-04/video/sub/x.mp4',
    '2026-03-04/x.mp4',
    'x.mp4',
    'latest.json',
    'HISTORY.JSON',
    '2026-02-30/video/x.mp4',
    '2026-3-4/video/x.mp4',
    'snapshot/latest/x.json',
    '2026-03-04/video/x%20y.mp4',
    `2026-03-04/video/${'a'.repeat(200)}.mp4`,
    '',
  ]
  for (const path of refused) {
    it(`refuses ${JSON.stringify(path)}`, async () => {
      const res = await call(env, `/media/${path}`, { token: await mint(signer, { email: 'v@example.com' }) })
      expect(res.status).toBe(400)
      expect(await res.json()).toEqual({ error: 'invalid media path' })
      expect(bucket.calls).toEqual([])
    })
  }

  it('only ever reads keys under media/', async () => {
    for (const path of ['history.json', VIDEO_PATH, '2026-03-04/video/missing.png']) await media(path)
    expect(bucket.calls.every((c) => c.key.startsWith('media/'))).toBe(true)
  })
})
