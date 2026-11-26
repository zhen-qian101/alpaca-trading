/** Drive the Worker's fetch handler directly, as the Workers runtime would. */
import worker from '../index.ts'
import { ACCESS_JWT_HEADER } from '../auth.ts'
import { DEV_PEER_HEADER, DEV_PEER_KEY_HEADER } from '../devPeer.ts'

/** A synthetic deployed origin. */
export const ORIGIN = 'https://alpaca-dashboard.unit-test.workers.dev'
/** What `vite dev` serves on. */
export const LOCAL_ORIGIN = 'http://localhost:5173'
/** The peer address `vite dev` stamps for a request from this machine. */
export const LOCAL_PEER = '127.0.0.1'
/** A synthetic dev-server key, as `vite dev` puts in the Worker env and stamps on each request. */
export const DEV_KEY = 'd'.repeat(64)

const ctx = {} as ExecutionContext

export interface CallInit extends RequestInit {
  /** Sent as Cf-Access-Jwt-Assertion. */
  token?: string
  origin?: string
  /** Sent as X-Alpaca-Dev-Peer, the TCP peer the Vite dev server stamps. */
  peer?: string
  /** Sent as X-Alpaca-Dev-Peer-Key; defaults to DEV_KEY whenever `peer` is given. */
  peerKey?: string | null
}

export function call(env: Env, path: string, init: CallInit = {}): Promise<Response> {
  const { token, origin = ORIGIN, peer, peerKey, ...rest } = init
  const headers = new Headers(rest.headers)
  if (token !== undefined) headers.set(ACCESS_JWT_HEADER, token)
  if (peer !== undefined) headers.set(DEV_PEER_HEADER, peer)
  const key = peerKey === undefined ? (peer !== undefined ? DEV_KEY : null) : peerKey
  if (key !== null) headers.set(DEV_PEER_KEY_HEADER, key)
  const request = new Request(`${origin}${path}`, { ...rest, headers }) as Request<unknown, IncomingRequestCfProperties>
  return worker.fetch(request, env, ctx)
}
