/**
 * The deploy's leak gate (scripts/schemaGate.ts, run by scripts/check-portfolio.ts):
 * it accepts the fixture, refuses what the page's own check refuses, reports
 * JSON pointers and rules but never a value or an undeclared key, and refuses
 * a schema it cannot enforce in full.
 */
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, describe, expect, it } from 'vitest'

import { parsePortfolio } from '../src/data/portfolio.ts'
import { checkSchema, formatProblem, MAX_PROBLEMS, SchemaError, validate } from './schemaGate.ts'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const readJson = (path: string): unknown => JSON.parse(readFileSync(join(root, path), 'utf8'))
const SCHEMA = readJson('schema/portfolio.schema.json') as Record<string, unknown>
const fixture = () => structuredClone(readJson('fixtures/portfolio.json')) as Record<string, any>

const CANARY = 'PA9CANARY77'

describe('the real schema', () => {
  it('is one the gate can enforce in full', () => {
    expect(() => checkSchema(SCHEMA)).not.toThrow()
  })

  it('accepts the synthetic fixture', () => {
    expect(validate(fixture(), SCHEMA)).toEqual([])
  })

  it('accepts every section unavailable at once', () => {
    const f = fixture()
    for (const key of ['account', 'trades', 'signals', 'strategy', 'recaps']) f[key] = null
    f.positions = { equities: null, options: null }
    f.meta.unavailable = ['account', 'positions', 'options', 'trades', 'signals', 'strategy', 'recaps']
    expect(validate(f, SCHEMA)).toEqual([])
    expect(parsePortfolio(f).ok).toBe(true)
  })
})

// Each mutation must be refused by this gate AND by the page's own check, at
// the pointer given. The value written is the canary where a string fits, so
// the output can be searched for it.
const MUTATIONS: [string, string, (f: Record<string, any>) => void][] = [
  ['extra top-level field', '', (f) => (f[CANARY] = 1)],
  ['extra field in meta', '/meta', (f) => (f.meta.account_number = CANARY)],
  ['extra field deep in a list', '/account/daily/3', (f) => (f.account.daily[3].price = 101.5)],
  ['current price on a position', '/positions/equities/0', (f) => (f.positions.equities[0].current_price = 12.5)],
  ['quote on an option', '/positions/options/0', (f) => (f.positions.options[0].bid = 1.2)],
  ['research note on a signal', '/signals/rows/0', (f) => (f.signals.rows[0].notes = CANARY)],
  ['reason on a contingency', '/signals/rows', (f) => {
    const row = f.signals.rows.find((r: { contingencies: unknown[] }) => r.contingencies.length)
    row.contingencies[0].reason = CANARY
  }],
  ['bars on a strategy symbol', '/strategy/symbols/0', (f) => (f.strategy.symbols[0].bars = [])],
  ['zh title on a recap', '/recaps/0', (f) => (f.recaps[0].title_zh = CANARY)],
  ['wrong schema version', '/meta/schema', (f) => (f.meta.schema = 2)],
  ['string where a number goes', '/account/daily/2/equity', (f) => (f.account.daily[2].equity = CANARY)],
  ['bad timestamp', '/meta/as_of', (f) => (f.meta.as_of = `2026-01-01 ${CANARY}`)],
  ['bad YouTube id', '/recaps/0/youtube_id', (f) => (f.recaps[0].youtube_id = `${CANARY}?x`)],
  // A language model writes the recap titles, so the file has no field for one.
  ['title on a recap', '/recaps/0', (f) => (f.recaps[0].title = CANARY)],
  ['unknown bias', '/signals/rows/0/bias', (f) => (f.signals.rows[0].bias = CANARY)],
  ['too many recaps', '/recaps', (f) => (f.recaps = Array.from({ length: 13 }, () => f.recaps[0]))],
  ['negative fill price', '/trades/round_trips/0/entry_price', (f) => (f.trades.round_trips[0].entry_price = -1)],
  ['fraction above 1', '/strategy/symbols/0/stop_pct', (f) => (f.strategy.symbols[0].stop_pct = 6)],
  ['duplicate unavailable section', '/meta/unavailable', (f) => (f.meta.unavailable = ['trades', 'trades'])],
  ['missing required field', '/account', (f) => delete f.account.equity],
  ['array where an object goes', '/positions', (f) => (f.positions = [])],
]

describe('a file the schema refuses', () => {
  const out = (problems: ReturnType<typeof validate>) => problems.map(formatProblem).join('\n')

  it.each(MUTATIONS)('%s', (_name, pointer, mutate) => {
    const f = fixture()
    mutate(f)
    const problems = validate(f, SCHEMA)
    expect(problems.length).toBeGreaterThan(0)
    expect(problems.map((p) => p.pointer).some((p) => p === pointer || p.startsWith(`${pointer}/`))).toBe(true)
    expect(parsePortfolio(f).ok, 'the page refuses it too').toBe(false)
    expect(out(problems)).not.toContain(CANARY)
    for (const p of problems) expect(p.pointer).toMatch(/^(\/[a-z_]+|\/[0-9]+)*$/)
  })

  it('reports an undeclared key at its parent, never by name', () => {
    const f = fixture()
    f.meta[CANARY] = CANARY
    expect(validate(f, SCHEMA).map(formatProblem)).toEqual(['/meta: has a property the schema does not declare'])
  })

  it('stops at MAX_PROBLEMS', () => {
    const f = fixture()
    f.account.daily = f.account.daily.map(() => ({ date: CANARY, equity: CANARY }))
    expect(validate(f, SCHEMA).length).toBe(MAX_PROBLEMS)
  })
})

describe('JSON Schema semantics', () => {
  const closed = (properties: Record<string, unknown>) => ({
    type: 'object',
    properties,
    required: Object.keys(properties),
    additionalProperties: false,
  })
  const run = (schema: Record<string, unknown>, value: unknown) => {
    checkSchema(schema)
    return validate(value, schema)
  }

  it('escapes property names in pointers (RFC 6901)', () => {
    const schema = closed({ 'a/b~c': { type: 'integer' } })
    expect(run(schema, { 'a/b~c': 'x' })).toEqual([{ pointer: '/a~1b~0c', rule: 'expected integer' }])
  })

  it('tells integers from numbers and booleans from 1', () => {
    expect(run({ type: 'integer' }, 2.0)).toEqual([])
    expect(run({ type: 'integer' }, 2.5)).toHaveLength(1)
    expect(run({ const: 1 }, true)).toHaveLength(1)
    expect(run({ enum: [1, 'a'] }, 1.0)).toEqual([])
    expect(run({ type: 'number' }, Number.NaN)).toHaveLength(1)
    expect(run({ type: 'number' }, Number.POSITIVE_INFINITY)).toHaveLength(1)
  })

  it('applies bounds, lengths in code points, and patterns with the u flag', () => {
    expect(run({ type: 'number', exclusiveMinimum: 0 }, 0)).toHaveLength(1)
    expect(run({ type: 'number', exclusiveMaximum: 1 }, 1)).toHaveLength(1)
    expect(run({ type: 'number', minimum: 0, maximum: 1 }, 1)).toEqual([])
    expect(run({ type: 'string', maxLength: 1 }, '😀')).toEqual([])
    expect(run({ type: 'string', pattern: '^[^\\u0000-\\u001F]*$' }, 'ok')).toEqual([])
    expect(run({ type: 'string', pattern: '^[^\\u0000-\\u001F]*$' }, 'a\nb')).toHaveLength(1)
  })

  it('compares objects in uniqueItems regardless of key order', () => {
    expect(run({ type: 'array', uniqueItems: true }, [{ a: 1, b: 2 }, { b: 2, a: 1 }])).toHaveLength(1)
    expect(run({ type: 'array', uniqueItems: true }, [1, true])).toEqual([])
  })

  it('needs exactly one oneOf branch', () => {
    const schema = { oneOf: [{ type: 'number' }, { type: 'integer' }] }
    expect(run(schema, 1)).toEqual([{ pointer: '', rule: 'matches 2 of oneOf (exactly 1 required)' }])
    expect(run(schema, 1.5)).toEqual([])
  })

  it('reports the closest anyOf branch for a nullable field', () => {
    const schema = closed({ n: { anyOf: [{ type: 'null' }, { type: 'number', minimum: 0 }] } })
    expect(run(schema, { n: -1 }).map(formatProblem)).toEqual(['/n: matches none of anyOf', '/n: below the minimum'])
    expect(run(schema, { n: null })).toEqual([])
  })
})

describe('a schema the gate cannot enforce', () => {
  const refuse = (schema: unknown, message: RegExp) => expect(() => checkSchema(schema)).toThrow(message)

  it.each([
    ['not an object', [], /not a JSON object/],
    ['an unsupported keyword', { type: 'string', format: 'email' }, /unsupported keyword/],
    ['a misspelt keyword', { type: 'object', additionalProperty: false }, /unsupported keyword/],
    ['an open object', { type: 'object', properties: {} }, /additionalProperties: false/],
    ['an object open by true', { type: 'object', properties: {}, additionalProperties: true }, /additionalProperties: false/],
    ['an open nested object', { type: 'array', items: { type: 'object' } }, /additionalProperties: false/],
    ['a dangling $ref', { $ref: '#/$defs/missing' }, /\$ref/],
    ['a remote $ref', { $ref: 'https://example.com/s.json' }, /\$ref/],
    ['a pattern that does not compile', { type: 'string', pattern: '(' }, /pattern/],
    ['required naming an undeclared property', { type: 'object', properties: {}, required: ['x'], additionalProperties: false }, /required/],
    ['an empty enum', { enum: [] }, /enum/],
    ['an empty anyOf', { anyOf: [] }, /anyOf/],
    ['an unknown type', { type: 'date' }, /unknown type/],
    ['a bound that is not a number', { type: 'number', maximum: '5' }, /maximum/],
    ['an open object inside $defs', { $defs: { x: { type: 'object' } }, type: 'null' }, /additionalProperties/],
  ])('refuses %s', (_name, schema, message) => {
    refuse(schema, message)
    expect(() => checkSchema(schema)).toThrow(SchemaError)
  })
})

describe('check-portfolio (the CLI the deploy runs)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'portfolio-gate-'))
  afterAll(() => rmSync(dir, { recursive: true, force: true }))

  const cli = (...args: string[]) => {
    const r = spawnSync(process.execPath, [join(root, 'scripts/check-portfolio.ts'), ...args], {
      encoding: 'utf8',
      cwd: root,
    })
    return { code: r.status, out: `${r.stdout}${r.stderr}` }
  }
  const write = (name: string, text: string) => {
    const path = join(dir, name)
    writeFileSync(path, text)
    return path
  }

  it('passes the fixture', () => {
    const r = cli(join(root, 'fixtures/portfolio.json'))
    expect(r).toEqual({ code: 0, out: 'valid: the file passes the schema gate\n' })
  })

  it('refuses an invalid file with pointers only (exit 1)', () => {
    const f = fixture()
    f.meta[CANARY] = CANARY
    f.account.daily[2].equity = CANARY
    const r = cli(write('bad.json', JSON.stringify(f)))
    expect(r.code).toBe(1)
    expect(r.out).toContain('/meta: has a property the schema does not declare')
    expect(r.out).toContain('/account/daily/2/equity: expected number')
    expect(r.out).not.toContain(CANARY)
  })

  it('refuses text that is not JSON without quoting it (exit 1)', () => {
    const r = cli(write('broken.json', `{"meta": ${CANARY}}`))
    expect(r).toEqual({ code: 1, out: 'refused: the file could not be read as JSON\n' })
  })

  it('refuses a missing file and an oversized one (exit 1)', () => {
    expect(cli(join(dir, 'nope.json')).code).toBe(1)
    const r = cli(write('big.json', `{"pad": "${'x'.repeat(4 * 1024 * 1024)}"}`))
    expect(r.code).toBe(1)
    expect(r.out).toMatch(/larger than/)
  })

  it('fails closed without a usable schema (exit 2)', () => {
    const data = join(root, 'fixtures/portfolio.json')
    expect(cli(data, '--schema', join(dir, 'missing.schema.json')).code).toBe(2)
    const open = write('open.schema.json', JSON.stringify({ type: 'object' }))
    const r = cli(data, '--schema', open)
    expect(r.code).toBe(2)
    expect(r.out).toMatch(/additionalProperties: false/)
  })

  it('needs exactly one file (exit 2)', () => {
    expect(cli().code).toBe(2)
    expect(cli('a.json', 'b.json').code).toBe(2)
  })
})
