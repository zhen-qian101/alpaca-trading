/**
 * Cloudflare Access verification for every /api and /media request.
 *
 * Access sits in front of the whole Worker at the edge, but the Worker does
 * not trust that alone: it verifies the `Cf-Access-Jwt-Assertion` header
 * itself (RS256 against the team's published certs, audience = the Access
 * application's AUD tag, issuer = the team domain). `ctx.access` is not
 * available to a Worker with Static Assets, so the header is the only source.
 *
 * FAIL CLOSED. When ACCESS_TEAM_DOMAIN or ACCESS_AUD is unset, every request
 * is refused, unless DEV_AUTH_BYPASS is exactly "1" AND the request comes
 * straight from this machine through `vite dev` (devBypassAllowed below):
 *   - it carries the dev server's key, which matches the Worker's DEV_PEER_KEY
 *     var (only `vite dev` has one), and its TCP peer is loopback, both stamped
 *     by devserver/peerStamp.ts over whatever a client sent, so this holds
 *     even when the dev server listens on the network (worker/devPeer.ts);
 *   - it was not relayed by a proxy or tunnel (no Forwarded / X-Forwarded-For
 *     / X-Real-IP / True-Client-IP, and CF-Connecting-IP, if any, loopback);
 *   - it is addressed to a loopback host (localhost, 127.0.0.1, [::1]).
 * The Host check alone proves nothing (a client picks its Host header); it
 * stays as a second lock for the deployed case, where the edge never routes
 * "localhost" and always sets CF-Connecting-IP to the real client. The
 * variable may only come from apps/dashboard/.dev.vars. Once Access IS
 * configured the bypass is ignored and the JWT is required.
 *
 * Neither the token nor any claim value is ever logged or put in a response.
 */
import { createRemoteJWKSet, errors, jwtVerify, type JWTPayload } from 'jose'

import {
  DEV_PEER_HEADER,
  DEV_PEER_KEY_HEADER,
  DEV_PEER_KEY_MIN_LENGTH,
  isLoopbackAddress,
  sameKey,
} from './devPeer.ts'

/** The header Cloudflare Access adds to every request it lets through. */
export const ACCESS_JWT_HEADER = 'Cf-Access-Jwt-Assertion'

/** Identity recorded as `requested_by` when the dev bypass is in use. */
export const DEV_BYPASS_IDENTITY = 'dev-bypass@localhost'

/** Access tokens are a few KB; anything far larger is not one. */
const MAX_TOKEN_LENGTH = 16 * 1024
/** Seconds of clock skew allowed on exp / nbf / iat. */
const CLOCK_TOLERANCE_S = 30
const MAX_IDENTITY_LENGTH = 320

export type AuthResult =
  | {
      ok: true
      /** JWT `email`, else `common_name` (service token); null when the token carries neither. */
      identity: string | null
      via: 'access' | 'dev-bypass'
    }
  | { ok: false; status: 401 | 503; error: string }

export interface AccessConfig {
  /** "https://<team>.cloudflareaccess.com", exactly as Access writes `iss`. */
  issuer: string
  certsUrl: URL
  audience: string
}

type ConfigState = { state: 'unset' } | { state: 'invalid' } | { state: 'ok'; config: AccessConfig }

/**
 * Read the Access settings. ACCESS_TEAM_DOMAIN may be given with or without
 * "https://" and a trailing slash; anything else (another scheme, a path, a
 * query, credentials) is a misconfiguration and refuses every request.
 */
export function readAccessConfig(env: Pick<Env, 'ACCESS_TEAM_DOMAIN' | 'ACCESS_AUD'>): ConfigState {
  const domain = typeof env.ACCESS_TEAM_DOMAIN === 'string' ? env.ACCESS_TEAM_DOMAIN.trim() : ''
  const audience = typeof env.ACCESS_AUD === 'string' ? env.ACCESS_AUD.trim() : ''
  if (!domain || !audience) return { state: 'unset' }

  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(domain) ? domain : `https://${domain}`
  let url: URL
  try {
    url = new URL(withScheme)
  } catch {
    return { state: 'invalid' }
  }
  if (
    url.protocol !== 'https:' ||
    url.username !== '' ||
    url.password !== '' ||
    url.port !== '' ||
    url.search !== '' ||
    url.hash !== '' ||
    url.pathname !== '/'
  ) {
    return { state: 'invalid' }
  }
  return {
    state: 'ok',
    config: {
      issuer: url.origin,
      certsUrl: new URL('/cdn-cgi/access/certs', url.origin),
      audience,
    },
  }
}

// One remote key set per certs URL, kept for the life of the isolate. jose
// caches the fetched keys (refetching after 10 minutes, or sooner when a token
// names an unknown `kid`, at most once per 30 s), so a warm isolate verifies
// without any network call.
const keySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>()

function keySetFor(certsUrl: URL): ReturnType<typeof createRemoteJWKSet> {
  let set = keySets.get(certsUrl.href)
  if (!set) {
    set = createRemoteJWKSet(certsUrl, { timeoutDuration: 5_000 })
    keySets.set(certsUrl.href, set)
  }
  return set
}

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])

function isLoopbackHost(url: URL): boolean {
  return LOOPBACK_HOSTS.has(url.hostname) || url.hostname.endsWith('.localhost')
}

/** Headers a proxy or tunnel adds; a request carrying one did not come straight from a local client. */
const FORWARDING_HEADERS = ['Forwarded', 'X-Forwarded-For', 'X-Real-IP', 'True-Client-IP'] as const

/**
 * May DEV_AUTH_BYPASS serve this request? Only when it comes straight from a
 * loopback peer (see the module comment). The values are never logged.
 */
export function devBypassAllowed(request: Request, env: Pick<Env, 'DEV_PEER_KEY'>, url: URL): boolean {
  const key = typeof env.DEV_PEER_KEY === 'string' ? env.DEV_PEER_KEY : ''
  if (key.length < DEV_PEER_KEY_MIN_LENGTH) return false // not `vite dev`: nothing stamped the request
  if (!sameKey(request.headers.get(DEV_PEER_KEY_HEADER), key)) return false
  if (!isLoopbackAddress(request.headers.get(DEV_PEER_HEADER))) return false
  if (FORWARDING_HEADERS.some((name) => request.headers.has(name))) return false
  const connecting = request.headers.get('CF-Connecting-IP')
  if (connecting !== null && !isLoopbackAddress(connecting)) return false
  return isLoopbackHost(url)
}

/** jose errors that mean "this token is not acceptable" rather than "could not check". */
const REJECTED_TOKEN_CODES = new Set([
  errors.JWTExpired.code,
  errors.JWTClaimValidationFailed.code,
  errors.JWTInvalid.code,
  errors.JWSInvalid.code,
  errors.JWSSignatureVerificationFailed.code,
  errors.JWKSNoMatchingKey.code,
  errors.JWKSMultipleMatchingKeys.code,
  errors.JOSEAlgNotAllowed.code,
  errors.JOSENotSupported.code,
  errors.JWKInvalid.code,
])

function identityOf(payload: JWTPayload): string | null {
  for (const claim of ['email', 'common_name'] as const) {
    const value = payload[claim]
    if (typeof value === 'string') {
      const trimmed = value.trim()
      if (trimmed && trimmed.length <= MAX_IDENTITY_LENGTH) return trimmed
    }
  }
  return null
}

export async function authenticate(request: Request, env: Env, url: URL): Promise<AuthResult> {
  const access = readAccessConfig(env)

  if (access.state === 'unset') {
    if (env.DEV_AUTH_BYPASS === '1') {
      if (devBypassAllowed(request, env, url)) {
        return { ok: true, identity: DEV_BYPASS_IDENTITY, via: 'dev-bypass' }
      }
      console.warn('access: DEV_AUTH_BYPASS applies only under vite dev, to a loopback peer; refusing')
    }
    console.error('access: ACCESS_TEAM_DOMAIN / ACCESS_AUD not set; refusing (fail closed)')
    return { ok: false, status: 503, error: 'authentication is not configured' }
  }
  if (access.state === 'invalid') {
    console.error('access: ACCESS_TEAM_DOMAIN is not an https origin; refusing (fail closed)')
    return { ok: false, status: 503, error: 'authentication is not configured' }
  }

  const token = request.headers.get(ACCESS_JWT_HEADER)?.trim() ?? ''
  if (!token) return { ok: false, status: 401, error: 'missing Access token' }
  if (token.length > MAX_TOKEN_LENGTH) return { ok: false, status: 401, error: 'invalid Access token' }

  const { issuer, audience, certsUrl } = access.config
  try {
    const { payload } = await jwtVerify(token, keySetFor(certsUrl), {
      issuer,
      audience,
      algorithms: ['RS256'],
      requiredClaims: ['exp'],
      clockTolerance: CLOCK_TOLERANCE_S,
    })
    return { ok: true, identity: identityOf(payload), via: 'access' }
  } catch (err) {
    const code = err instanceof errors.JOSEError ? err.code : null
    if (code !== null && REJECTED_TOKEN_CODES.has(code)) {
      console.warn(`access: token rejected (${code})`)
      return { ok: false, status: 401, error: 'invalid Access token' }
    }
    // Could not fetch or parse the team's certs: refuse, but say it is ours.
    console.error(`access: cannot verify tokens (${code ?? (err instanceof Error ? err.name : typeof err)})`)
    return { ok: false, status: 503, error: 'cannot verify identity right now' }
  }
}
