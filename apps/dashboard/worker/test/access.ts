/**
 * A fake Cloudflare Access for tests: RSA keys generated at test time, a
 * certs endpoint served by a stubbed global fetch, and tokens minted on
 * demand. No token or key is ever written to disk (CI runs gitleaks).
 */
import { exportJWK, generateKeyPair, SignJWT, type JWK, type JWTPayload } from 'jose'
import { vi } from 'vitest'

/** A synthetic team domain (".example" never resolves). */
export const TEAM_DOMAIN = 'https://unit-test-team.cloudflareaccess.example'
/** A synthetic 64-hex-char Access AUD tag. */
export const AUD = '0123456789abcdef'.repeat(4)

export interface Signer {
  kid: string
  privateKey: CryptoKey
  jwk: JWK
}

export async function makeSigner(kid: string): Promise<Signer> {
  const { publicKey, privateKey } = await generateKeyPair('RS256', { extractable: true })
  const jwk: JWK = { ...(await exportJWK(publicKey)), kid, alg: 'RS256', use: 'sig' }
  return { kid, privateKey, jwk }
}

export interface MintOptions {
  iss?: string
  aud?: string | string[]
  /** Seconds from now; negative = already expired. Pass null to omit exp. */
  expIn?: number | null
  /** Seconds from now for nbf. */
  nbfIn?: number
  kid?: string
}

/** A token shaped like an Access application token. */
export async function mint(signer: Signer, claims: JWTPayload = {}, opts: MintOptions = {}): Promise<string> {
  const now = Math.floor(Date.now() / 1000)
  const jwt = new SignJWT({ type: 'app', identity_nonce: 'synthetic', country: 'XX', ...claims })
    .setProtectedHeader({ alg: 'RS256', kid: opts.kid ?? signer.kid })
    .setIssuer(opts.iss ?? TEAM_DOMAIN)
    .setAudience(opts.aud ?? [AUD])
    .setSubject('00000000-0000-4000-8000-000000000000')
    .setIssuedAt(now)
  if (opts.expIn !== null) jwt.setExpirationTime(now + (opts.expIn ?? 600))
  if (opts.nbfIn !== undefined) jwt.setNotBefore(now + opts.nbfIn)
  return jwt.sign(signer.privateKey)
}

export interface CertsEndpoint {
  fetch: ReturnType<typeof vi.fn>
  /** Requests made to each URL. */
  hits: (url: string) => number
}

/**
 * Stub global fetch so that `<domain>/cdn-cgi/access/certs` returns the given
 * keys (or `status` with no keys). Any other URL fails the test loudly.
 */
export function serveCerts(byDomain: Record<string, { keys: JWK[] } | { status: number }>): CertsEndpoint {
  const counts = new Map<string, number>()
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    counts.set(href, (counts.get(href) ?? 0) + 1)
    for (const [domain, answer] of Object.entries(byDomain)) {
      if (href === `${domain}/cdn-cgi/access/certs`) {
        if ('status' in answer) return new Response('unavailable', { status: answer.status })
        return Response.json({ keys: answer.keys, public_cert: { kid: 'x', cert: 'unused' } })
      }
    }
    throw new Error(`unexpected fetch in a Worker test: ${href}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetch: fetchMock, hits: (url) => counts.get(url) ?? 0 }
}

/** The Env the Worker gets in production: Access configured. */
export function accessEnv(data: R2Bucket, overrides: Partial<Env> = {}): Env {
  return { DATA: data, ACCESS_TEAM_DOMAIN: TEAM_DOMAIN, ACCESS_AUD: AUD, ...overrides }
}
