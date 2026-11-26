/**
 * Invariants of the workflows that build and deploy the public site
 * (.github/workflows/portfolio.yml and keepalive.yml), read from the files
 * as text: pinned actions, least privilege, secrets only through env/with,
 * a build job that never sees a secret and a deploy job that installs
 * nothing from the app's lockfile, the gate before the stage before the
 * deploy, and a modest schedule. The private dashboard's dashboard.yml runs
 * in the same public repository, so its logs are public too: it gets the
 * generic checks and the quiet-wrangler rule. actionlint checks the syntax;
 * this checks the decisions.
 */
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { CADENCE } from '../src/lib/copy.ts'
import { NORMAL_REFRESH_HOURS, STALE_AFTER_MS } from '../src/lib/freshness.ts'

const repo = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..')
const read = (path: string) => readFileSync(join(repo, path), 'utf8')
const portfolio = read('.github/workflows/portfolio.yml')
const keepalive = read('.github/workflows/keepalive.yml')
const dashboard = read('.github/workflows/dashboard.yml')
const workflows = { portfolio, keepalive, dashboard }

const PINS: Record<string, string> = {
  'actions/checkout': '3d3c42e5aac5ba805825da76410c181273ba90b1',
  'actions/setup-node': '949feb2413d6458794dcd2491c4babbbce0c15c1',
  'pnpm/action-setup': '73e46992e05b19abd31f2f3c20e0e7370d762115',
  'actions/upload-artifact': 'cf430e030ddbb5b0abf93d22962f4752f3646cd9',
  'actions/download-artifact': '9000827ccba6bdab643e8b6fd33ac0654aef8333',
}

/** The deploy job's `if:`, which the build's artifact upload must repeat exactly. */
const DEPLOYS = `vars.PORTFOLIO_DEPLOY == 'true' &&
      github.ref == 'refs/heads/main' &&
      (github.event_name == 'push' || github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')`

/** The steps of a job, split at each `- name:` or `- uses:` line. */
function steps(jobText: string): string[] {
  return jobText.split(/\n {6}- (?=name:|uses:)/).slice(1)
}

/** The text of one top-level job: from `  <name>:` to the next job or the end. */
function job(text: string, name: string): string {
  const start = text.indexOf(`\n  ${name}:\n`)
  expect(start, `job ${name}`).toBeGreaterThan(0)
  const next = text.slice(start + 1).search(/\n {2}[a-z][\w-]*:\n/)
  return next < 0 ? text.slice(start) : text.slice(start, start + 1 + next)
}

describe.each(Object.entries(workflows))('%s.yml', (_name, text) => {
  it('pins every action to a full commit SHA, at the agreed versions', () => {
    const uses = [...text.matchAll(/^\s+(?:- )?uses: (.+)$/gm)].map((m) => m[1]!)
    for (const line of uses) {
      const m = /^([\w.-]+\/[\w.-]+)@([0-9a-f]{40}) # v\d+\.\d+\.\d+$/.exec(line)
      expect(m, line).not.toBeNull()
      expect(PINS[m![1]!], `${m![1]} is an agreed action`).toBe(m![2])
    }
  })

  it('never runs on pull_request_target, never traces shell, never prints a file', () => {
    expect(text).not.toMatch(/pull_request_target/)
    expect(text).not.toMatch(/set -[a-wyz]*x/)
    expect(text).not.toMatch(/\b(cat|head|tail|less|xxd|od)\s/)
  })

  it('reads secrets only into step env or action inputs, never into a script', () => {
    for (const line of text.split('\n').filter((l) => l.includes('secrets.'))) {
      expect(line, line).toMatch(/^\s+[A-Za-z_][A-Za-z0-9_]*: \$\{\{ secrets\.[A-Z0-9_]+ \}\}$/)
    }
  })

  it('checks out without persisting the token', () => {
    const checkouts = text.match(/uses: actions\/checkout@/g) ?? []
    const persisted = text.match(/persist-credentials: false/g) ?? []
    expect(persisted.length).toBe(checkouts.length)
  })
})

describe('portfolio.yml', () => {
  it('is read-only at the top and grants nothing more to any job', () => {
    expect(portfolio).toMatch(/\npermissions:\n {2}contents: read\n\n/)
    expect(portfolio.match(/permissions:/g)).toHaveLength(1)
  })

  it('builds and tests on pull requests and on main, on the agreed paths', () => {
    for (const path of ['apps/portfolio/**', 'web/public_export.py', '.github/workflows/portfolio.yml'])
      expect(portfolio.split(`- ${path}`).length - 1, path).toBe(2)
    const build = job(portfolio, 'build')
    expect(build).toMatch(/pnpm install --frozen-lockfile/)
    for (const step of ['pnpm lint', 'pnpm typecheck', 'pnpm test', 'pnpm build'])
      expect(build, step).toContain(`run: ${step}\n`)
    expect(build).toContain('node scripts/check-portfolio.ts fixtures/portfolio.json')
    // The deployed site carries the licences of what it bundles.
    const names = steps(build).map((s) => s.split('\n')[0]!)
    const built = names.indexOf('name: Build')
    expect(built).toBeGreaterThan(0)
    expect(names[built + 1]).toBe('name: Licences in the build')
    expect(build).toContain('run: node scripts/check-licenses.ts dist\n')
    expect(build).toContain('node scripts/stage-data.ts --from fixtures/portfolio.json --dist "$rehearsal" --quiet')
  })

  it('builds with no secret, no environment and no data, and hands the deploy a build without data', () => {
    const build = job(portfolio, 'build')
    expect(build).not.toMatch(/secrets\.|environment:|\$\{\{ *env\./)
    // The check for dist/data comes after the build and before the upload;
    // the fixture rehearsal comes after the upload, on a copy.
    const names = steps(build).map((s) => s.split('\n')[0]!)
    const order = [
      'name: Build',
      'name: Build output has no data file',
      'name: Hand the built site to the deploy job',
      "name: Rehearse the deploy's gates on the fixture",
    ].map((n) => names.indexOf(n))
    expect(order.every((i) => i > 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    const upload = steps(build).find((s) => s.startsWith('name: Hand the built site'))!
    expect(upload).toContain('uses: actions/upload-artifact@')
    expect(upload).toContain(`if: >-\n          ${DEPLOYS.replaceAll('\n      ', '\n          ')}\n`)
    expect(upload).toMatch(/name: portfolio-site\n\s+path: apps\/portfolio\/dist\n\s+if-no-files-found: error\n\s+retention-days: 1\n/)
    const rehearsal = steps(build).find((s) => s.startsWith('name: Rehearse'))!
    expect(rehearsal).toContain('cp -R dist "$rehearsal"')
    expect(rehearsal).toContain('test ! -e dist/data')
    expect(rehearsal).not.toMatch(/--dist dist|stage-data --from fixtures\/portfolio\.json --quiet/)
  })

  it('deploys without installing anything from the app’s lockfile, and runs wrangler alone at the locked version', () => {
    const deploy = job(portfolio, 'deploy')
    // Comments may name the lockfile; the job's code may not use it.
    const code = deploy
      .split('\n')
      .filter((l) => !/^\s*#/.test(l))
      .join('\n')
    expect(code).not.toMatch(/pnpm|npm ci|frozen-lockfile|^\s+cache: |cache-dependency-path/m)
    expect(deploy).toContain('package-manager-cache: false')
    // The one install: wrangler, by exact version, into the deploy directory only.
    const installs = deploy.match(/npm (?:install|i) [^\n]*/g) ?? []
    expect(installs).toHaveLength(1)
    expect(installs[0]).toContain('--prefix "$site"')
    expect(deploy).toContain('"wrangler@${WRANGLER_VERSION}"')
    expect(deploy).toContain("--before=\"$(date -u -d '7 days ago'")
    expect(deploy).toContain('test ! -e "$site/package.json"')
    // The deploy runs that copy directly, in the deploy directory: no action
    // that could install its own, and nothing npx could fetch.
    const run = steps(deploy).find((s) => s.startsWith('name: Deploy\n'))!
    expect(run).not.toMatch(/uses:/)
    expect(run).toContain('site="$RUNNER_TEMP/$SITE_DIR"')
    expect(run).toContain('cd "$site"')
    expect(run).toContain('./node_modules/.bin/wrangler deploy')
    expect(code).not.toMatch(/\bnpx\b|pnpm exec|wrangler-action/)
    const pinned = /WRANGLER_VERSION: "(\d+\.\d+\.\d+)"/.exec(deploy)?.[1]
    const lock = readFileSync(join(repo, 'apps/portfolio/pnpm-lock.yaml'), 'utf8')
    const locked = /\n {6}wrangler:\n {8}specifier: [^\n]+\n {8}version: (\d+\.\d+\.\d+)/.exec(lock)?.[1]
    expect(locked).toBeDefined()
    expect(pinned).toBe(locked)
    // No secret in the install step's environment.
    const install = steps(deploy).find((s) => s.startsWith('name: Install wrangler alone'))!
    expect(install).not.toContain('secrets.')
  })

  it('ships a re-serialisation of the checked value, never the downloaded bytes', () => {
    const deploy = job(portfolio, 'deploy')
    expect(deploy).toContain('node scripts/stage-data.ts --from "$RUNNER_TEMP/$DATA_FILE" --dist "$site/dist" --quiet')
    expect(deploy).toContain('rm -f "${RUNNER_TEMP:?}/${DATA_FILE:?}"')
    // The downloaded file is never copied or moved anywhere.
    expect(deploy).not.toMatch(/(cp|mv|install|rsync)\s[^\n]*\$RUNNER_TEMP\/\$DATA_FILE/)
    const download = steps(deploy).find((s) => s.startsWith('name: Download the built site'))!
    expect(download).toContain('uses: actions/download-artifact@')
    expect(download).toContain('name: portfolio-site')
    expect(download).toContain('path: ${{ runner.temp }}/${{ env.SITE_DIR }}/dist')
    expect(deploy).toContain('test ! -e "$site/dist/data"')
  })

  it('deploys from main only, through the portfolio environment, one at a time', () => {
    const deploy = job(portfolio, 'deploy')
    expect(deploy).toContain('needs: build')
    expect(deploy).toContain(`if: >-\n      ${DEPLOYS}\n`)
    expect(deploy).toContain("github.ref == 'refs/heads/main'")
    expect(deploy).toMatch(/environment:\n\s+name: portfolio\n/)
    expect(deploy).toMatch(/concurrency:\n\s+group: portfolio-deploy\n\s+cancel-in-progress: false/)
  })

  it('downloads with the read-only R2 token, then gates, then stages, then deploys', () => {
    const deploy = job(portfolio, 'deploy')
    const order = [
      'name: Check for credentials',
      'name: Download the built site',
      'name: The build carries no data',
      'name: Download portfolio.json from R2',
      'node scripts/check-portfolio.ts',
      'node scripts/stage-data.ts --from',
      'name: Compare with the live site',
      'name: Install wrangler alone',
      './node_modules/.bin/wrangler deploy',
    ].map((s) => deploy.indexOf(s))
    expect(order.every((i) => i > 0)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(deploy).toContain('AWS_ACCESS_KEY_ID: ${{ secrets.R2_PUBLIC_READ_KEY_ID }}')
    expect(deploy).toContain('AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_PUBLIC_READ_SECRET }}')
    // The bucket's account and the Worker's account are separate settings, so
    // the site can be deployed from a Cloudflare account of its own.
    expect(deploy).toContain('R2_ACCOUNT: ${{ secrets.R2_PUBLIC_ACCOUNT_ID }}')
    const run = steps(deploy).find((s) => s.startsWith('name: Deploy\n'))!
    expect(run).toContain('CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}')
    expect(run).toContain('CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}')
    expect(run).not.toMatch(/R2_|AWS_/)
    expect(deploy).not.toMatch(/R2_ACCOUNT: \$\{\{ secrets\.CLOUDFLARE_ACCOUNT_ID/)
    expect(deploy).toContain('BUCKET: alpaca-public')
    expect(deploy).toContain('.r2.cloudflarestorage.com')
    expect(deploy).toContain('--only-show-errors')
  })

  it('skips the deploy, and says so, unless all five credentials are set', () => {
    const deploy = job(portfolio, 'deploy')
    // Every step after the check runs only when it said ready.
    for (const step of steps(deploy).slice(1)) expect(step, step.split('\n')[0]).toMatch(/\n\s+if: steps\.creds\.outputs\.ready == 'true'/)
    expect(deploy).toContain('::notice title=Deploy skipped::Not set in the portfolio environment')
    const creds = deploy.split('name: Check for credentials')[1]!.split('- uses:')[0]!
    for (const name of [
      'CLOUDFLARE_API_TOKEN',
      'CLOUDFLARE_ACCOUNT_ID',
      'R2_PUBLIC_ACCOUNT_ID',
      'R2_PUBLIC_READ_KEY_ID',
      'R2_PUBLIC_READ_SECRET',
    ]) {
      expect(creds, name).toContain(`${name}: \${{ secrets.${name} }}`)
      expect(creds, name).toMatch(new RegExp(`for name in [^\\n]*\\b${name}\\b`))
    }
  })

  it('runs at most 22 times on any day of the week', () => {
    const crons = [...portfolio.matchAll(/- cron: "([^"]+)"/g)].map((m) => m[1]!)
    expect(crons.length).toBeGreaterThan(0)
    const perDay = Array.from({ length: 7 }, (_, dow) => crons.reduce((n, c) => n + runsOn(c, dow), 0))
    expect(Math.max(...perDay)).toBeLessThanOrEqual(22)
    expect(Math.min(...perDay)).toBeGreaterThanOrEqual(4)
  })
})

describe('wrangler with a Cloudflare token, in a public log', () => {
  // On an authentication error wrangler runs `whoami`, which logs (at level
  // "log") the email of the token's user and a table of every account name
  // and id the token can see. None of them is a secret GitHub would mask.
  // At WRANGLER_LOG=warn only warnings and errors print, and the error still
  // does. `wrangler --version` prints at "log" too, so the level is set on
  // the deploy step alone, never job- or workflow-wide.
  const deploySteps = Object.entries({ portfolio, dashboard }).map(([name, text]) => {
    const step = steps(job(text, 'deploy')).find((s) => s.startsWith('name: Deploy\n'))
    return [name, step ?? ''] as const
  })

  it.each(deploySteps)('%s.yml deploys with wrangler quiet and no third-party action', (_name, step) => {
    expect(step).toMatch(/wrangler deploy\b/)
    expect(step).not.toMatch(/uses:/)
    expect(step).toMatch(/\n {10}CLOUDFLARE_API_TOKEN: \$\{\{ secrets\.CLOUDFLARE_API_TOKEN \}\}\n/)
    expect(step).toMatch(/\n {10}WRANGLER_LOG: warn\n/)
    expect(step).toMatch(/\n {10}WRANGLER_WRITE_LOGS: "false"\n/)
  })

  it.each(Object.entries(workflows))('%s.yml never raises wrangler’s log level or asks who the token is', (_name, text) => {
    expect(text).not.toMatch(/cloudflare\/wrangler-action/)
    expect(text).not.toMatch(/wrangler whoami/)
    for (const m of text.matchAll(/WRANGLER_LOG: *"?([a-z]+)"?/g)) expect(['warn', 'error', 'none']).toContain(m[1])
    // Every step that holds the token is a deploy step checked above, or the
    // credentials check, which only tests that it is set.
    for (const step of steps(text).filter((s) => s.includes('secrets.CLOUDFLARE_API_TOKEN'))) {
      const name = step.split('\n')[0]!
      expect(['name: Deploy', 'name: Check for credentials', 'name: Check for Cloudflare credentials'], name).toContain(name)
    }
  })
})

describe('the cadence the site states', () => {
  /** Every scheduled run in one week, as minutes from Sunday 00:00 UTC. */
  const week = () => {
    const crons = [...portfolio.matchAll(/- cron: "([^"]+)"/g)].map((m) => m[1]!)
    const runs: number[] = []
    for (const cron of crons) {
      const [minute, hour, , , weekday] = cron.split(' ')
      for (const d of expand(weekday!, 6))
        for (const h of expand(hour!, 23)) for (const m of expand(minute!, 59)) runs.push(d * 1440 + h * 60 + m)
    }
    return [...new Set(runs)].sort((a, b) => a - b)
  }

  it('never leaves more than the stated hours between deploys, weekends included', () => {
    const runs = week()
    const gaps = runs.map((t, i) => (i ? t - runs[i - 1]! : t + 7 * 1440 - runs.at(-1)!))
    expect(Math.max(...gaps)).toBeLessThanOrEqual(NORMAL_REFRESH_HOURS * 60)
    expect(CADENCE).toContain(`about every ${NORMAL_REFRESH_HOURS} hours at other times`)
  })

  it('deploys every 30 minutes through the US session on weekdays, as stated', () => {
    const runs = new Set(week())
    // 13:37 to 21:07 UTC covers 9:30 to 16:00 New York time in EST and EDT.
    for (let d = 1; d <= 5; d++)
      for (let t = 13 * 60 + 37; t <= 21 * 60 + 7; t += 30) expect(runs.has(d * 1440 + t), `${d} ${t}`).toBe(true)
    expect(CADENCE).toContain('about every 30 minutes during US market hours on weekdays')
  })

  it('calls figures stale only well past the longest healthy gap', () => {
    // The longest gap, plus the exporter's hourly heartbeat and 15-minute cron,
    // with room for three scheduled runs lost in a row.
    expect(STALE_AFTER_MS).toBeGreaterThanOrEqual((4 * NORMAL_REFRESH_HOURS + 1.25) * 3_600_000)
    expect(STALE_AFTER_MS).toBeLessThanOrEqual(36 * 3_600_000)
  })
})

describe('keepalive.yml', () => {
  it('grants nothing at the top and only contents: write to its one job', () => {
    expect(keepalive).toMatch(/\npermissions: \{\}\n/)
    expect(keepalive.match(/permissions:/g)).toHaveLength(2)
    expect(keepalive).toMatch(/ {4}permissions:\n {6}contents: write # [^\n]+\n {4}env:/)
    expect(keepalive).not.toMatch(/uses:/)
  })

  it('commits only after the idle threshold, well inside the 60 days', () => {
    const days = Number(/IDLE_DAYS: "(\d+)"/.exec(keepalive)?.[1])
    expect(days).toBeGreaterThan(0)
    // Weekly runs: the latest it can act is days + 6, and that must be < 60.
    expect(days + 6).toBeLessThan(60)
    expect(keepalive).toMatch(/- cron: "\d+ \d+ \* \* \d"/)
  })
})

/** How many times a five-field cron (numbers, lists, ranges, `*`) fires on weekday `dow` (0 = Sunday). */
function runsOn(cron: string, dow: number): number {
  const [minute, hour, dom, month, weekday] = cron.split(' ')
  expect(dom, cron).toBe('*')
  expect(month, cron).toBe('*')
  const count = (field: string, max: number) => expand(field!, max).length
  return expand(weekday!, 6).includes(dow) ? count(minute!, 59) * count(hour!, 23) : 0
}

function expand(field: string, max: number): number[] {
  if (field === '*') return Array.from({ length: max + 1 }, (_, i) => i)
  return field.split(',').flatMap((part) => {
    const [a, b] = part.split('-').map(Number)
    expect(Number.isInteger(a), field).toBe(true)
    return b === undefined ? [a!] : Array.from({ length: b - a! + 1 }, (_, i) => a! + i)
  })
}
