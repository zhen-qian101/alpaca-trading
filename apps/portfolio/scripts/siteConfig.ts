/**
 * Build-time helpers for site.config.json (see src/lib/siteConfig.ts for the
 * fields): read and check it, and render the <head> tags that depend on it.
 */
import { readFileSync } from 'node:fs'

import { checkSiteConfig, type SiteConfig } from '../src/lib/siteConfig.ts'

export function readSiteConfig(path: string): SiteConfig {
  return checkSiteConfig(JSON.parse(readFileSync(path, 'utf8')))
}

const escapeHtml = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

/** The <head> tags that depend on the config. */
export function pageHeadTags(config: SiteConfig): string {
  const t = escapeHtml(config.title)
  const d = escapeHtml(config.description)
  const tags = [
    `<title>${t}</title>`,
    `<meta name="description" content="${d}" />`,
    `<meta property="og:type" content="website" />`,
    `<meta property="og:site_name" content="${t}" />`,
    `<meta property="og:title" content="${t}" />`,
    `<meta property="og:description" content="${d}" />`,
    `<meta name="twitter:title" content="${t}" />`,
    `<meta name="twitter:description" content="${d}" />`,
  ]
  if (config.siteUrl) {
    const origin = escapeHtml(config.siteUrl)
    tags.push(
      `<link rel="canonical" href="${origin}/" />`,
      `<meta property="og:url" content="${origin}/" />`,
      `<meta property="og:image" content="${origin}/og-image.png" />`,
      `<meta property="og:image:width" content="1200" />`,
      `<meta property="og:image:height" content="630" />`,
      `<meta property="og:image:alt" content="${t}: a ledger-style chart of a paper trading account's equity" />`,
      `<meta name="twitter:card" content="summary_large_image" />`,
    )
  } else {
    tags.push(`<meta name="twitter:card" content="summary" />`)
  }
  if (config.author.name) tags.push(`<meta name="author" content="${escapeHtml(config.author.name)}" />`)
  return tags.join('\n    ')
}
