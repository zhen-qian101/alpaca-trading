/**
 * Alpaca dashboard Worker. Static Assets serve the SPA; this handler receives
 * /api/* and /media/* (wrangler.jsonc `assets.run_worker_first`), plus any
 * non-navigation request that matched no asset.
 *
 *   GET  /api/snapshot                          snapshot/latest.json + approval_request
 *   GET  /api/chart?symbol=SYM                  chart/<chartKey(SYM)>.json
 *   GET  /api/look/asset?date=D&kind=K          look/<D>/<K>.png
 *   POST /api/look/approve  {date, approved}    writes approvals/<date>.json
 *   GET  /media/<path>                          media/<path>, Range-capable
 *
 * (HEAD is accepted wherever GET is.) Every /api and /media request is
 * authenticated first (auth.ts, fail closed); the R2 layout and payload shapes
 * are the contract in src/contract.ts. Responses always carry the security
 * headers in http.ts; API JSON is never cached.
 *
 * Status codes. Every non-2xx from /api and /media is JSON `{error}` (ApiError):
 *   401  missing or invalid Access token
 *   503  Access not configured (fail closed), or its certs unreachable;
 *        also "object changed while being read; retry" on a racing media read
 *   400  malformed query, media path or approve body
 *   403  approve: no X-Requested-With: alpaca-dashboard, cross-origin, or a
 *        token without email / common_name
 *   404  unknown route, or the R2 object is not published (yet)
 *   405  wrong method (Allow header lists the right ones)
 *   413  approve body over 1 KiB
 *   415  approve without Content-Type: application/json
 *   416  unsatisfiable Range (its Content-Range carries the full size)
 *   422  approve date outside [ET today - 1, ET today + 3]
 *   502  snapshot/latest.json is not a JSON object
 * Successes: 200; 202 + ApproveResponse for an approval (recorded, applied
 * later by the home box); 206 for a Range; 304 on a matching If-None-Match.
 * With the dev bypass active every response also carries X-Dev-Auth-Bypass: 1.
 */
import { authenticate } from './auth.ts'
import { apiError, applySecurityHeaders, describeError, methodNotAllowed, withoutBody } from './http.ts'
import { getChart, getLookAsset, getMedia, getSnapshot, postApprove } from './routes.ts'

const READ_METHODS = ['GET', 'HEAD'] as const
const isRead = (method: string) => method === 'GET' || method === 'HEAD'

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url)
  const { pathname } = url
  const isApi = pathname.startsWith('/api/')
  const isMedia = pathname.startsWith('/media/')
  if (!isApi && !isMedia) return apiError(404, 'not found')

  const auth = await authenticate(request, env, url)
  if (!auth.ok) return apiError(auth.status, auth.error)

  const response = await dispatch(request, env, url, auth.identity)
  if (auth.via === 'dev-bypass') response.headers.set('X-Dev-Auth-Bypass', '1')
  return response
}

async function dispatch(request: Request, env: Env, url: URL, identity: string | null): Promise<Response> {
  const { method } = request
  const { pathname } = url

  if (pathname.startsWith('/media/')) {
    return isRead(method) ? getMedia(request, env, url) : methodNotAllowed(READ_METHODS)
  }

  switch (pathname) {
    case '/api/snapshot':
      if (!isRead(method)) return methodNotAllowed(READ_METHODS)
      return method === 'HEAD' ? withoutBody(await getSnapshot(env)) : getSnapshot(env)
    case '/api/chart':
      if (!isRead(method)) return methodNotAllowed(READ_METHODS)
      return method === 'HEAD' ? withoutBody(await getChart(env, url)) : getChart(env, url)
    case '/api/look/asset':
      return isRead(method) ? getLookAsset(request, env, url) : methodNotAllowed(READ_METHODS)
    case '/api/look/approve':
      if (method !== 'POST') return methodNotAllowed(['POST'])
      return postApprove(request, env, url, identity, new Date())
    default:
      return apiError(404, 'not found')
  }
}

export default {
  async fetch(request, env, _ctx) {
    let response: Response
    try {
      response = await route(request, env)
    } catch (err) {
      // Class and code only: never the message, the request or its headers.
      console.error(`worker: unhandled error on ${request.method} (${describeError(err)})`)
      response = apiError(500, 'internal error')
    }
    applySecurityHeaders(response.headers)
    return response
  },
} satisfies ExportedHandler<Env>
