/**
 * public/_headers sets the CSP for the SPA's static assets (index.html and
 * /assets/*; Worker responses set their own in worker/http.ts, and serve no
 * HTML). The recap player needs exactly one third-party allowance: frames
 * from the YouTube no-cookie domain. This holds the whole policy to its exact
 * string, so widening it (another frame source, a YouTube script or image
 * host, a relaxed directive) has to change this test on purpose.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { EMBED_ORIGIN } from '../src/lib/youtube.ts'

const APP = join(dirname(fileURLToPath(import.meta.url)), '..')
const HEADERS_FILE = readFileSync(join(APP, 'public', '_headers'), 'utf8')

const EXPECTED_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "media-src 'self' blob:",
  'frame-src https://www.youtube-nocookie.com',
  "font-src 'self'",
  "connect-src 'self'",
  "worker-src 'self' blob:",
  "manifest-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  'upgrade-insecure-requests',
].join('; ')

/** The headers of one path block ("/*"): every indented "Name: value" line under it. */
function block(path: string): Map<string, string> {
  const headers = new Map<string, string>()
  let inside = false
  for (const line of HEADERS_FILE.split('\n')) {
    if (line.startsWith('#') || line.trim() === '') continue
    if (!/^\s/.test(line)) {
      inside = line.trim() === path
      continue
    }
    if (!inside) continue
    const colon = line.indexOf(':')
    headers.set(line.slice(0, colon).trim().toLowerCase(), line.slice(colon + 1).trim())
  }
  return headers
}

function directives(csp: string): Map<string, string[]> {
  return new Map(
    csp
      .split(';')
      .map((d) => d.trim().split(/\s+/))
      .filter((parts) => parts[0] !== '')
      .map(([name, ...values]) => [name, values]),
  )
}

describe('static asset headers', () => {
  const headers = block('/*')
  const csp = headers.get('content-security-policy') ?? ''

  it('is exactly the expected CSP', () => {
    expect(csp).toBe(EXPECTED_CSP)
  })

  it('allows frames from the YouTube no-cookie player and nothing else', () => {
    const d = directives(csp)
    expect(d.get('frame-src')).toEqual([EMBED_ORIGIN])
    expect(EMBED_ORIGIN).toBe('https://www.youtube-nocookie.com')
    // No other directive names any YouTube or Google host: no script, no
    // thumbnails (the poster is the R2 cover), no API calls.
    for (const [name, values] of d) {
      if (name === 'frame-src') continue
      for (const v of values) expect(v, `${name} ${v}`).not.toMatch(/youtube|ytimg|google|https?:/i)
    }
    // child-src would be the fallback for frames; it must not reappear wider.
    expect(d.has('child-src')).toBe(false)
  })

  it('keeps the page itself unframeable and referrer-free', () => {
    expect(headers.get('x-frame-options')).toBe('DENY')
    expect(headers.get('referrer-policy')).toBe('no-referrer')
    expect(headers.get('cross-origin-opener-policy')).toBe('same-origin')
    // COEP require-corp would block the cross-origin player frame.
    expect(headers.has('cross-origin-embedder-policy')).toBe(false)
  })
})
