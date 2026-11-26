import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

import { completeLicenseFile } from './scripts/licenseNotices.ts'
import { pageHeadTags, readSiteConfig } from './scripts/siteConfig.ts'
import { LICENSES_FILE } from './src/lib/licenses.ts'

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))

/**
 * Serves /data/portfolio.json in `vite dev` only. The built site carries NO
 * data: the deploy step copies the exporter's file into dist/data/ (see
 * scripts/stage-data.ts). By default this serves the synthetic fixture;
 * PORTFOLIO_DATA=/path/to/portfolio.json points it at a real export kept
 * outside the repository.
 */
function devPortfolioData(): Plugin {
  return {
    name: 'portfolio-dev-data',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use('/data/portfolio.json', (_req, res) => {
        const file = process.env.PORTFOLIO_DATA || here('./fixtures/portfolio.json')
        try {
          const body = readFileSync(file)
          res.setHeader('Content-Type', 'application/json; charset=utf-8')
          res.setHeader('Cache-Control', 'no-cache')
          res.end(body)
        } catch {
          res.statusCode = 404
          res.end('not found')
        }
      })
    },
  }
}

/** Title, description and social tags come from site.config.json. */
function siteHead(): Plugin {
  return {
    name: 'portfolio-site-head',
    transformIndexHtml(html) {
      const config = readSiteConfig(here('./site.config.json'))
      return html.replace('<!--site-head-->', pageHeadTags(config))
    },
  }
}

/**
 * Completes the licences file `build.license` writes (src/lib/licenses.ts)
 * with what the npm packages leave out: lightweight-charts' NOTICE and
 * fancy-canvas' MIT text (scripts/licenseNotices.ts). Runs after Vite's own
 * licence step, and fails the build when that step wrote nothing, a bundled
 * package has no licence text, or a supplied text is for another version.
 */
function licenseNotices(): Plugin {
  return {
    name: 'portfolio-license-notices',
    apply: 'build',
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const file = bundle[LICENSES_FILE]
        if (file?.type !== 'asset') this.error(`build.license wrote no ${LICENSES_FILE}`)
        const source = typeof file.source === 'string' ? file.source : new TextDecoder().decode(file.source)
        const { text, problems } = completeLicenseFile(source)
        if (problems.length > 0) this.error(`${LICENSES_FILE}: ${problems.join('; ')}`)
        file.source = text
      },
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), devPortfolioData(), siteHead(), licenseNotices()],
  build: {
    // The public bundle ships without source maps.
    sourcemap: false,
    // The licences of every bundled package, at the site root so the Worker
    // serves it (src/lib/licenses.ts); the footer links to it.
    license: { fileName: LICENSES_FILE },
  },
})
