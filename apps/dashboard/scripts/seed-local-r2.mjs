#!/usr/bin/env node
// Load the synthetic fixtures into the LOCAL R2 simulation that `pnpm dev`
// (Vite + workerd) reads, laid out exactly as the publisher lays out the real
// bucket (see src/contract.ts). Nothing here talks to Cloudflare: every write
// is `wrangler r2 object put --local`, which lands in .wrangler/state.
//
//   pnpm seed:local                  snapshot + charts as of NOW, look re-dated
//                                    to today's US/Eastern session date
//   pnpm seed:local --as-is          upload the fixtures unchanged (2026-03-04)
//   pnpm seed:local --with-approval  also write a pending approval request
//   pnpm seed:local --from-dir DIR   upload a `python -m web.publisher --out-dir DIR`
//                                    tree instead of the fixtures, key for key
//   pnpm seed:local --persist-to DIR use DIR as the local state instead of
//                                    .wrangler/state; start `pnpm dev` with
//                                    ALPACA_DASHBOARD_STATE_DIR=DIR to serve it.
//                                    Use this for REAL data from --from-dir, so
//                                    it never lands inside the repository.
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, extname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const FIX = join(APP, 'fixtures')
const BUCKET = 'alpaca-dashboard'
const WRANGLER = join(APP, 'node_modules', 'wrangler', 'bin', 'wrangler.js')

const argv = process.argv.slice(2)
function valueOf(flag) {
  const i = argv.indexOf(flag)
  if (i === -1) return null
  const v = argv[i + 1]
  if (!v || v.startsWith('--')) throw new Error(`${flag} needs a directory`)
  return resolve(v)
}
const args = new Set(argv)
const asIs = args.has('--as-is')
const withApproval = args.has('--with-approval')
const fromDir = valueOf('--from-dir')
const persistTo = valueOf('--persist-to')

const tmp = mkdtempSync(join(tmpdir(), 'alpaca-seed-'))
const readJson = (p) => JSON.parse(readFileSync(p, 'utf8'))
const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00')
const etToday = () =>
  new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date())

/** The publisher's look image URL: versioned by the image's content (web/publisher.py look_asset_url). */
const lookAssetUrl = (date, kind) => {
  const v = createHash('sha256').update(readFileSync(join(FIX, 'look', `${kind}.png`))).digest('hex').slice(0, 12)
  return `/api/look/asset?date=${date}&kind=${kind}&v=${v}`
}

function put(key, file, contentType) {
  execFileSync(
    process.execPath,
    [WRANGLER, 'r2', 'object', 'put', `${BUCKET}/${key}`, '--file', file,
      '--content-type', contentType, '--local', ...(persistTo ? ['--persist-to', persistTo] : [])],
    { cwd: APP, stdio: ['ignore', 'ignore', 'inherit'], env: { ...process.env, WRANGLER_SEND_METRICS: 'false' } },
  )
  console.log(`  ${key}`)
}

function putJson(key, value) {
  const file = join(tmp, key.replaceAll('/', '__'))
  writeFileSync(file, JSON.stringify(value))
  put(key, file, 'application/json')
}

const TYPES = {
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.mp4': 'video/mp4',
}
const KEY_PREFIXES = ['snapshot/', 'chart/', 'look/', 'media/', 'approvals/']

/** Every file under `root` (dot-files skipped: the publisher's temp files). */
function walk(root) {
  const out = []
  for (const name of readdirSync(root)) {
    if (name.startsWith('.')) continue
    const p = join(root, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

/** Upload a publisher --out-dir tree as is: its paths ARE the R2 keys. */
function seedFromDir(dir) {
  let n = 0
  for (const file of walk(dir).sort()) {
    const key = relative(dir, file).split(sep).join('/')
    const type = TYPES[extname(key).toLowerCase()]
    if (!type || !KEY_PREFIXES.some((p) => key.startsWith(p))) {
      console.warn(`  skipped ${key} (not part of the bucket layout)`)
      continue
    }
    put(key, file, type)
    n += 1
  }
  if (n === 0) throw new Error(`${dir} holds no bucket objects; run the publisher with --out-dir first`)
}

try {
  console.log(`seeding local R2 bucket "${BUCKET}" (${persistTo ?? '.wrangler/state'})`)
  if (fromDir) {
    seedFromDir(fromDir)
    console.log('done')
    process.exit(0)
  }

  // snapshot + look images
  const snapshot = readJson(join(FIX, 'snapshot.json'))
  if (!asIs) {
    snapshot.as_of = nowIso()
    const look = snapshot.look
    if (look && !('error' in look)) {
      const today = etToday()
      const old = look.date
      look.date = today
      look.session_date = today
      look.dates = [today, ...look.dates.filter((d) => d !== old && d !== today)]
      if (look.exists) {
        // Re-dated, and versioned by content like the publisher's URLs.
        look.plate_url = lookAssetUrl(today, 'plate')
        look.keyframe_url = lookAssetUrl(today, 'keyframe')
      }
    }
  }
  putJson('snapshot/latest.json', snapshot)
  const lookDate = snapshot.look.date
  put(`look/${lookDate}/plate.png`, join(FIX, 'look', 'plate.png'), 'image/png')
  put(`look/${lookDate}/keyframe.png`, join(FIX, 'look', 'keyframe.png'), 'image/png')

  if (withApproval) {
    const req = readJson(join(FIX, 'approval-request.json'))
    putJson(`approvals/${lookDate}.json`, { ...req, date: lookDate, requested_at: nowIso() })
  }

  // charts: fixtures/chart-<KEY>.json -> chart/<KEY>.json
  for (const name of readdirSync(FIX).filter((n) => /^chart-.+\.json$/.test(n))) {
    const chart = readJson(join(FIX, name))
    if (!asIs) chart.as_of = nowIso()
    putJson(`chart/${name.slice('chart-'.length)}`, chart)
  }

  // history + the media it points at (one sample video and cover reused).
  // Like the real bucket: a YouTube edition's mp4 is NOT in R2 (it plays from
  // YouTube); only a fallback edition's `briefing` is. Nothing zh exists.
  const history = readJson(join(FIX, 'history.json'))
  if (/_zh|"zh"|[\u4e00-\u9fff]/.test(JSON.stringify(history))) {
    throw new Error('fixtures/history.json carries zh: the Chinese cut never goes to the cloud')
  }
  putJson('media/history.json', history)
  const URL_KEYS = ['briefing', 'cover']
  const uploaded = new Set()
  for (const ed of history.briefings) {
    const urls = [...URL_KEYS.map((k) => ed[k]), ...ed.clips.map((c) => c.file)].filter(Boolean)
    for (const url of urls) {
      // "/media/<date>/<slot>/<file>?v=<sha>": the key is the path, without the version
      const key = url.replace(/^\//, '').replace(/\?.*$/, '')
      if (uploaded.has(key)) continue
      uploaded.add(key)
      if (key.endsWith('.mp4')) put(key, join(FIX, 'media', 'sample.mp4'), 'video/mp4')
      else put(key, join(FIX, 'media', 'cover.png'), 'image/png')
    }
    // meta.json = the archived meta: names only, no URLs, no YouTube id
    // (history.json carries that; meta.json's raw `youtube` record is dropped)
    const meta = structuredClone(ed)
    for (const k of [...URL_KEYS, 'youtube_id', 'local']) delete meta[k]
    for (const c of meta.clips) delete c.file
    putJson(`media/${ed.date}/${ed.slot}/meta.json`, meta)
  }
  console.log('done')
} finally {
  rmSync(tmp, { recursive: true, force: true })
}
