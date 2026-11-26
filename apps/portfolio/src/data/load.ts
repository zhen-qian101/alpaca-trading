/**
 * Loads /data/portfolio.json: a static file next to the page, placed into
 * dist/data/ when the site is deployed (served from the synthetic fixture in
 * `vite dev`). Same origin only, so the page makes no other request.
 */
import { parsePortfolio, type Portfolio } from './portfolio.ts'

export const DATA_URL = '/data/portfolio.json'

export type LoadState =
  | { status: 'loading' }
  /** Nothing published yet, or the request failed. */
  | { status: 'missing' }
  /** A file is there but it is not a portfolio this page can trust. */
  | { status: 'invalid'; errors: string[] }
  | { status: 'ready'; data: Portfolio }

export async function loadPortfolio(fetchImpl: typeof fetch = fetch): Promise<Exclude<LoadState, { status: 'loading' }>> {
  let response: Response
  try {
    response = await fetchImpl(DATA_URL, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      // Revalidate every time: a deploy replaces the file under the same name.
      cache: 'no-cache',
    })
  } catch {
    return { status: 'missing' }
  }
  if (!response.ok) return { status: 'missing' }

  // An unknown path is answered with index.html (single-page-app fallback),
  // which means "not published", not "corrupt".
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('text/html')) return { status: 'missing' }

  let json: unknown
  try {
    json = await response.json()
  } catch {
    return type.includes('json') ? { status: 'invalid', errors: ['$: not valid JSON'] } : { status: 'missing' }
  }
  const result = parsePortfolio(json)
  return result.ok ? { status: 'ready', data: result.value } : { status: 'invalid', errors: result.errors }
}
