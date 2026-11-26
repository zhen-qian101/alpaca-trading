/**
 * Deployment and security invariants of the public site, checked from the
 * files themselves: the Worker serves static assets only, the CSP is strict,
 * index.html has no inline code, and nothing comes from the private dashboard.
 */
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { checkSiteConfig } from '../src/lib/siteConfig.ts'
import { pageHeadTags } from './siteConfig.ts'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const read = (path: string) => readFileSync(join(root, path), 'utf8')

/** JSONC to JSON: drops // and block comments outside strings, and trailing commas. */
function parseJsonc(text: string): Record<string, unknown> {
  let out = ''
  let inString = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!
    if (inString) {
      out += c
      if (c === '\\') out += text[++i] ?? ''
      else if (c === '"') inString = false
    } else if (c === '"') {
      inString = true
      out += c
    } else if (c === '/' && text[i + 1] === '/') {
      while (i < text.length && text[i] !== '\n') i++
      out += '\n'
    } else if (c === '/' && text[i + 1] === '*') {
      i = text.indexOf('*/', i + 2) + 1
    } else out += c
  }
  return JSON.parse(out.replace(/,(\s*[}\]])/g, '$1')) as Record<string, unknown>
}

function cspDirectives(): Map<string, string[]> {
  const headers = read('public/_headers')
  const line = headers.split('\n').find((l) => l.trim().startsWith('Content-Security-Policy:'))
  expect(line, 'public/_headers sets a Content-Security-Policy').toBeDefined()
  const policy = line!.split('Content-Security-Policy:')[1]!.trim()
  return new Map(
    policy
      .split(';')
      .map((d) => d.trim().split(/\s+/))
      .filter((parts) => parts[0])
      .map(([name, ...values]) => [name!, values]),
  )
}

describe('wrangler.jsonc', () => {
  const config = parseJsonc(read('wrangler.jsonc'))

  it('is a static-assets-only Worker named alpaca-portfolio', () => {
    expect(config.name).toBe('alpaca-portfolio')
    expect(config).not.toHaveProperty('main')
    expect(config.assets).toEqual({
      directory: './dist',
      not_found_handling: 'single-page-application',
      html_handling: 'auto-trailing-slash',
    })
  })

  it('has no bindings, routes, variables or other keys', () => {
    // Anything beyond these could run code, reach data or add a hostname.
    const allowed = ['$schema', 'name', 'compatibility_date', 'workers_dev', 'preview_urls', 'assets']
    expect(Object.keys(config).filter((k) => !allowed.includes(k))).toEqual([])
    expect(config.workers_dev).toBe(true)
  })
})

describe('public/_headers', () => {
  const csp = cspDirectives()

  it('allows scripts and data from this origin only', () => {
    expect(csp.get('default-src')).toEqual(["'none'"])
    expect(csp.get('script-src')).toEqual(["'self'"])
    expect(csp.get('connect-src')).toEqual(["'self'"])
    expect(csp.get('img-src')).toEqual(["'self'", 'data:'])
    expect(csp.get('font-src')).toEqual(["'self'"])
    expect(csp.get('object-src')).toEqual(["'none'"])
    expect(csp.get('base-uri')).toEqual(["'none'"])
    expect(csp.get('frame-ancestors')).toEqual(["'none'"])
  })

  it('frames only the YouTube no-cookie player', () => {
    expect(csp.get('frame-src')).toEqual(['https://www.youtube-nocookie.com'])
  })

  it('never allows inline or eval', () => {
    const all = [...csp.values()].flat()
    expect(all).not.toContain("'unsafe-inline'")
    expect(all).not.toContain("'unsafe-eval'")
    expect(all).not.toContain("'unsafe-hashes'")
  })

  it('hashes exactly the style lightweight-charts injects for its attribution logo', () => {
    const lib = (file: string) => readFileSync(join(root, 'node_modules/lightweight-charts/dist', file), 'utf8')
    const prod = /innerText="(a#tv-attr-logo\{[^"]*)"/.exec(lib('lightweight-charts.production.mjs'))?.[1]
    const dev = /const css = `(a#tv-attr-logo\{[^`]*)`/.exec(lib('lightweight-charts.development.mjs'))?.[1]
    expect(prod, 'attribution style found in the production build').toBeDefined()
    expect(dev).toBe(prod)
    const hash = `'sha256-${createHash('sha256').update(prod!).digest('base64')}'`
    expect(csp.get('style-src')).toEqual(["'self'", hash])
  })

  it('sets the other security headers', () => {
    const headers = read('public/_headers')
    expect(headers).toMatch(/^\s+X-Content-Type-Options: nosniff$/m)
    expect(headers).toMatch(/^\s+Referrer-Policy: strict-origin-when-cross-origin$/m)
    expect(headers).toMatch(/^\s+X-Frame-Options: DENY$/m)
    expect(headers).toMatch(/^\s+Cross-Origin-Opener-Policy: same-origin$/m)
    expect(headers).toMatch(/^\s+Permissions-Policy: camera=\(\), microphone=\(\), geolocation=\(\)/m)
  })
})

describe('index.html', () => {
  const html = read('index.html')

  it('has no inline script or style', () => {
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)]
    expect(scripts.length).toBeGreaterThan(0)
    for (const [, attrs, body] of scripts) {
      expect(attrs).toMatch(/\bsrc="\//)
      expect(body!.trim()).toBe('')
    }
    expect(html).not.toMatch(/<style\b/)
    expect(html).not.toMatch(/\sstyle="/)
  })

  it('loads nothing from another origin', () => {
    expect(html).not.toMatch(/(src|href)="(https?:)?\/\//)
  })
})

describe('independence from the private dashboard', () => {
  const files = (dir: string): string[] =>
    readdirSync(join(root, dir)).flatMap((name) => {
      const path = join(dir, name)
      return statSync(join(root, path)).isDirectory() ? files(path) : [path]
    })

  it('imports nothing from apps/dashboard', () => {
    for (const file of [...files('src'), ...files('scripts'), 'vite.config.ts', 'vitest.config.ts']) {
      const text = read(file)
      expect(text, file).not.toMatch(/from\s+['"][^'"]*dashboard[^'"]*['"]/)
      expect(text, file).not.toMatch(/import\(\s*['"][^'"]*dashboard/)
    }
  })

  it('has its own package', () => {
    const pkg = JSON.parse(read('package.json')) as { name: string; dependencies: Record<string, string> }
    expect(pkg.name).toBe('alpaca-portfolio')
    expect(Object.keys(pkg.dependencies).sort()).toEqual(['lightweight-charts', 'react', 'react-dom'])
  })
})

describe('site.config.json', () => {
  it('is valid and carries the public title and repository', () => {
    const config = checkSiteConfig(JSON.parse(read('site.config.json')))
    expect(config.title).toBe('Alpaca Trading System')
    expect(config.repoUrl).toBe('https://github.com/zhen-qian101/alpaca-trading')
  })

  it('skips half-filled author links and refuses non-https ones', () => {
    const base = JSON.parse(read('site.config.json')) as Record<string, unknown>
    expect(checkSiteConfig(base).author.links).toEqual([])
    expect(() => checkSiteConfig({ ...base, author: { name: '', links: [{ label: 'Site', url: 'http://example.com' }] } })).toThrow(
      /https/,
    )
    expect(() => checkSiteConfig({ ...base, extra: true })).toThrow(/unknown key/)
    expect(() => checkSiteConfig({ ...base, siteUrl: 'https://example.com/' })).toThrow(/siteUrl/)
  })

  it('renders escaped head tags, with social image tags only once the site URL is known', () => {
    const base = checkSiteConfig(JSON.parse(read('site.config.json')))
    const tags = pageHeadTags({ ...base, title: 'A <b> & "c"' })
    expect(tags).toContain('<title>A &lt;b&gt; &amp; &quot;c&quot;</title>')
    expect(tags).not.toContain('og:image')
    const withUrl = pageHeadTags({ ...base, siteUrl: 'https://example.workers.dev' })
    expect(withUrl).toContain('<meta property="og:image" content="https://example.workers.dev/og-image.png" />')
    expect(withUrl).toContain('<link rel="canonical" href="https://example.workers.dev/" />')
  })
})
