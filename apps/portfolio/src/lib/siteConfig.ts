/**
 * site.config.json: the few things about this site its owner fills in.
 *
 *   title        page title and social title
 *   description  meta description and social description (keep it generic)
 *   siteUrl      the deployed origin, e.g. https://alpaca-portfolio.<sub>.workers.dev,
 *                with no trailing slash. Empty until the first deploy; while it
 *                is empty the canonical link, og:url and og:image are left out,
 *                because social cards need absolute URLs.
 *   repoUrl      the public repository
 *   author       a display name and links, shown in the footer only when filled
 *
 * Checked here, with no Node imports, so the same check runs at build time
 * (scripts/siteConfig.ts, for the <head>) and in the bundle (src/site.ts, for
 * the footer).
 */
export interface SiteLink {
  label: string
  url: string
}

export interface SiteConfig {
  title: string
  description: string
  siteUrl: string
  repoUrl: string
  author: { name: string; links: SiteLink[] }
}

function isHttpsUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password
  } catch {
    return false
  }
}

/** Throws with a readable message when the config is malformed. */
export function checkSiteConfig(raw: unknown): SiteConfig {
  const problems: string[] = []
  const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
  if (!isObj(raw)) throw new Error('site.config.json must be a JSON object')

  const known = new Set(['title', 'description', 'siteUrl', 'repoUrl', 'author'])
  for (const key of Object.keys(raw)) if (!known.has(key)) problems.push(`unknown key "${key}"`)

  const text = (key: string, value: unknown, { required = false, max = 300 } = {}) => {
    if (typeof value !== 'string') {
      problems.push(`${key} must be a string`)
      return ''
    }
    if (required && !value.trim()) problems.push(`${key} must not be empty`)
    if (value.length > max) problems.push(`${key} is longer than ${max} characters`)
    return value
  }

  const title = text('title', raw.title, { required: true, max: 80 })
  const description = text('description', raw.description, { required: true, max: 300 })
  const siteUrl = text('siteUrl', raw.siteUrl, { max: 200 })
  if (siteUrl && (!isHttpsUrl(siteUrl) || siteUrl.endsWith('/') || new URL(siteUrl).pathname !== '/'))
    problems.push('siteUrl must be an https origin with no path or trailing slash, or empty')
  const repoUrl = text('repoUrl', raw.repoUrl, { required: true, max: 200 })
  if (repoUrl && !isHttpsUrl(repoUrl)) problems.push('repoUrl must be an https URL')

  const author = isObj(raw.author) ? raw.author : {}
  if (!isObj(raw.author)) problems.push('author must be an object')
  for (const key of Object.keys(author)) if (key !== 'name' && key !== 'links') problems.push(`unknown key "author.${key}"`)
  const name = text('author.name', author.name, { max: 80 })
  const links: SiteLink[] = []
  if (!Array.isArray(author.links)) problems.push('author.links must be an array')
  else
    author.links.forEach((link: unknown, i) => {
      if (!isObj(link)) {
        problems.push(`author.links[${i}] must be an object`)
        return
      }
      const label = text(`author.links[${i}].label`, link.label, { max: 40 })
      const url = text(`author.links[${i}].url`, link.url, { max: 200 })
      if (url && !isHttpsUrl(url)) problems.push(`author.links[${i}].url must be an https URL`)
      // A half-filled placeholder is skipped, not shown.
      if (label.trim() && url.trim()) links.push({ label: label.trim(), url })
    })

  if (problems.length) throw new Error(`site.config.json: ${problems.join('; ')}`)
  return { title, description, siteUrl, repoUrl, author: { name: name.trim(), links } }
}
