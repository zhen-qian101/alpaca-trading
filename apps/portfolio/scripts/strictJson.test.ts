/**
 * Strict JSON reading (scripts/strictJson.ts): a duplicate key anywhere is
 * refused, so the value a gate checks is the only value the bytes can mean;
 * and stage-data ships a re-serialisation of that value, never the source
 * bytes (LEAK-4: a duplicate `data_delay` carried text past both gates).
 */
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, describe, expect, it } from 'vitest'

import { canonicalJson, decodeUtf8, JsonTextError, parseStrictJson } from './strictJson.ts'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const FIXTURE_TEXT = readFileSync(join(root, 'fixtures/portfolio.json'), 'utf8')
const CANARY = 'CANARY-SECRET account PA9CANARY0001 /home/canary/x'
/** The fixture with a first, unseen `data_delay` in meta: JSON.parse keeps the second. */
const SMUGGLED = FIXTURE_TEXT.replace('"meta": {', `"meta": {"data_delay": "${CANARY}", `)

describe('parseStrictJson', () => {
  it('reads what JSON.parse reads when no key repeats', () => {
    expect(parseStrictJson(FIXTURE_TEXT)).toEqual(JSON.parse(FIXTURE_TEXT))
    for (const text of ['0', '"a:b"', '[]', '{}', 'null', '{"a": {"a": {"a": 1}}}', '[{"k": 1}, {"k": 2}]'])
      expect(parseStrictJson(text)).toEqual(JSON.parse(text))
  })

  it('is not fooled by colons, quotes and backslashes inside strings', () => {
    const text = String.raw`{"a:b": "c:d", "e\":": "\\", "f": "\\\":", "g": ["x:y", {"h": ":"}]}`
    expect(parseStrictJson(text)).toEqual(JSON.parse(text))
  })

  it.each([
    ['at the top', '{"a": 1, "a": 1}'],
    ['in a nested object', '{"a": {"b": 1, "c": 2, "b": 1}}'],
    ['in an object inside an array', '[1, {"x": 1}, {"y": 1, "y": 2}]'],
    ['with an escaped spelling of the same key', String.raw`{"a": 1, "\u0061": 2}`],
    ['for __proto__', '{"__proto__": 1, "__proto__": 2}'],
    ['in the fixture', SMUGGLED],
  ])('refuses a duplicate key %s, and never quotes it', (_where, text) => {
    expect(() => JSON.parse(text)).not.toThrow()
    let error: unknown
    try {
      parseStrictJson(text)
    } catch (e) {
      error = e
    }
    expect(error).toBeInstanceOf(JsonTextError)
    expect((error as Error).message).toBe('an object has the same key twice')
  })

  it.each(['', '{', 'NaN', '{"a": Infinity}', "{'a': 1}", '{"a": 1} {"b": 2}', '﻿{}'])(
    'refuses %j without quoting the parser',
    (text) => {
      expect(() => parseStrictJson(text)).toThrow(new JsonTextError('not a JSON document'))
    },
  )

  it('refuses bytes that are not UTF-8', () => {
    expect(() => decodeUtf8(new Uint8Array([0x7b, 0xff, 0x7d]))).toThrow(JsonTextError)
    expect(decodeUtf8(new TextEncoder().encode('{"a": "é"}'))).toBe('{"a": "é"}')
  })

  it('serialises the value, and only the value', () => {
    const value = parseStrictJson(FIXTURE_TEXT)
    expect(JSON.parse(canonicalJson(value))).toEqual(value)
    expect(canonicalJson(value)).toBe(JSON.stringify(JSON.parse(FIXTURE_TEXT)))
  })
})

describe('the deploy scripts on a file with a duplicate key', () => {
  const dir = mkdtempSync(join(tmpdir(), 'portfolio-strict-'))
  afterAll(() => rmSync(dir, { recursive: true, force: true }))
  const node = (script: string, ...args: string[]) => {
    const r = spawnSync(process.execPath, [join(root, 'scripts', script), ...args], { encoding: 'utf8', cwd: root })
    return { code: r.status, out: `${r.stdout}${r.stderr}` }
  }
  const write = (name: string, text: string) => {
    const path = join(dir, name)
    writeFileSync(path, text)
    return path
  }
  /** A built site with no data, as the build job uploads it. */
  const site = (name: string) => {
    const dist = join(dir, name)
    mkdirSync(dist, { recursive: true })
    writeFileSync(join(dist, 'index.html'), '<!doctype html>')
    return dist
  }

  it('check-portfolio refuses it, naming neither the key nor the value (exit 1)', () => {
    const r = node('check-portfolio.ts', write('smuggled.json', SMUGGLED))
    expect(r).toEqual({ code: 1, out: 'refused: an object in the file has the same key twice\n' })
    expect(r.out).not.toContain('CANARY')
  })

  it('stage-data refuses it and writes nothing (exit 1)', () => {
    const dist = site('dup')
    const r = node('stage-data.ts', '--from', write('smuggled2.json', SMUGGLED), '--dist', dist, '--quiet')
    expect(r.code).toBe(1)
    expect(r.out).not.toContain('CANARY')
    expect(existsSync(join(dist, 'data'))).toBe(false)
  })

  it('stage-data writes the validated value, never the source bytes', () => {
    const dist = site('ok')
    // Same value, different bytes: whitespace, key order inside the text is kept, an escaped character.
    const source = write('pretty.json', `\n\n${FIXTURE_TEXT.replace('"FAKEyt00004"', String.raw`"\u0046AKEyt00004"`)}  \n`)
    const r = node('stage-data.ts', '--from', source, '--dist', dist, '--quiet')
    expect(r).toEqual({ code: 0, out: 'staged dist/data/portfolio.json\n' })
    expect(readdirSync(join(dist, 'data'))).toEqual(['portfolio.json'])
    const staged = readFileSync(join(dist, 'data', 'portfolio.json'), 'utf8')
    expect(staged).not.toBe(readFileSync(source, 'utf8'))
    expect(staged).toBe(JSON.stringify(JSON.parse(FIXTURE_TEXT)))
  })

  it('stage-data stages into the build it is pointed at, and refuses one with no index.html (exit 2)', () => {
    const empty = join(dir, 'empty')
    mkdirSync(empty)
    expect(node('stage-data.ts', '--from', join(root, 'fixtures/portfolio.json'), '--dist', empty).code).toBe(2)
    expect(node('stage-data.ts', '--from', join(root, 'fixtures/portfolio.json'), '--dist').code).toBe(2)
    const dist = site('counts')
    cpSync(join(root, 'fixtures/portfolio.json'), join(dir, 'f.json'))
    const r = node('stage-data.ts', '--from', join(dir, 'f.json'), '--dist', dist)
    expect(r.code).toBe(0)
    expect(r.out).toMatch(/^staged dist\/data\/portfolio\.json: \d+ daily points, \d+ round trips, 4 recaps\n$/)
  })
})
