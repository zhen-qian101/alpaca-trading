/**
 * Check that a built site carries the licences of what it bundles:
 *
 *   node scripts/check-licenses.ts [DIST]    (default: ./dist)
 *
 * DIST/third-party-licenses.txt (src/lib/licenses.ts) must exist, list every
 * runtime dependency in package.json, give every package it lists a licence
 * text (its own, or the one scripts/licenseNotices.ts supplies), and carry the
 * TradingView NOTICE. The build writes that file (vite.config.ts) and fails
 * when it cannot complete it; this checks the output that is deployed.
 *
 *   exit 0  the file is complete
 *   exit 1  the file is missing or incomplete
 *   exit 2  usage error, or DIST has no index.html
 *
 * Dependency-free (node: modules and this app's own TypeScript).
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { LICENSES_FILE } from '../src/lib/licenses.ts'
import { checkLicenseFile } from './licenseNotices.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function main(argv: string[]): number {
  if (argv.length > 1) {
    console.error('usage: check-licenses [DIST]')
    return 2
  }
  const dist = resolve(argv[0] ?? resolve(root, 'dist'))
  if (!existsSync(resolve(dist, 'index.html'))) {
    console.error('dist/ has no index.html: run `pnpm build` first')
    return 2
  }
  const file = resolve(dist, LICENSES_FILE)
  if (!existsSync(file)) {
    console.error(`refused: the build wrote no ${LICENSES_FILE}`)
    return 1
  }
  const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8')) as { dependencies?: Record<string, string> }
  const problems = checkLicenseFile(readFileSync(file, 'utf8'), Object.keys(pkg.dependencies ?? {}))
  if (problems.length > 0) {
    console.error(`refused: ${LICENSES_FILE} has ${problems.length} problem(s)`)
    for (const p of problems) console.error(`  ${p}`)
    return 1
  }
  console.log(`ok: ${LICENSES_FILE}`)
  return 0
}

process.exitCode = main(process.argv.slice(2))
