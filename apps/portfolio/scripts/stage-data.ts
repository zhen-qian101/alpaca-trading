/**
 * Stage a portfolio.json into the built site: reads it strictly (a duplicate
 * key anywhere is refused, scripts/strictJson.ts), checks it with the same
 * closed-shape check the page runs, then WRITES dist/data/portfolio.json as a
 * re-serialisation of the value that passed. The source bytes are never
 * copied: whatever they held that the checks did not see stays behind.
 *
 *   pnpm build
 *   pnpm stage-data --from /path/to/portfolio.json    # e.g. the exporter's --out-dir
 *   npx wrangler deploy                               # (owner only)
 *
 *   --dist DIR   the built site to stage into (default: ./dist). The deploy
 *                job stages into the build it downloaded.
 *   --quiet      print no counts (CI, whose logs are public).
 *
 * Refuses (exit 1) a file that fails a check, and (exit 2) a missing --from or
 * a dist with no index.html. Prints counts only, never values.
 *
 * Dependency-free (node: modules and this app's own TypeScript), so the
 * deploy job runs it without installing the app's packages.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { parsePortfolio } from '../src/data/portfolio.ts'
import { canonicalJson, decodeUtf8, JsonTextError, parseStrictJson } from './strictJson.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function option(argv: string[], name: string): string | undefined {
  const at = argv.indexOf(name)
  return at >= 0 ? argv[at + 1] : undefined
}

function main(argv: string[]): number {
  const from = option(argv, '--from')
  const quiet = argv.includes('--quiet')
  if (!from || (argv.includes('--dist') && !option(argv, '--dist'))) {
    console.error('usage: stage-data --from <portfolio.json> [--dist DIR] [--quiet]')
    return 2
  }
  const dist = resolve(option(argv, '--dist') ?? resolve(root, 'dist'))
  if (!existsSync(resolve(dist, 'index.html'))) {
    console.error('dist/ has no index.html: run `pnpm build` first')
    return 2
  }
  let parsed: unknown
  try {
    parsed = parseStrictJson(decodeUtf8(readFileSync(from)))
  } catch (err) {
    if (err instanceof JsonTextError && err.message.includes('same key'))
      console.error('refused: an object in the file has the same key twice')
    else console.error('could not read the file as JSON')
    return 1
  }
  const result = parsePortfolio(parsed)
  if (!result.ok) {
    console.error(`refused: ${result.errors.length} problem(s)`)
    for (const e of result.errors) console.error(`  ${e}`)
    return 1
  }
  const out = resolve(dist, 'data', 'portfolio.json')
  mkdirSync(dirname(out), { recursive: true })
  writeFileSync(out, canonicalJson(result.value))
  if (quiet) {
    console.log('staged dist/data/portfolio.json')
    return 0
  }
  const d = result.value
  console.log(
    `staged dist/data/portfolio.json: ${d.account?.daily.length ?? 0} daily points, ` +
      `${d.trades?.round_trips.length ?? 0} round trips, ${d.recaps?.length ?? 0} recaps`,
  )
  return 0
}

process.exitCode = main(process.argv.slice(2))
