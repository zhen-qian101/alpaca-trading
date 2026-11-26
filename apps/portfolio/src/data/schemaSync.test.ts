/**
 * The page's runtime check (src/data/portfolio.ts) must say exactly what the
 * exporter's gate (schema/portfolio.schema.json) says. This walks both trees
 * side by side and fails on any property, required flag, bound, pattern, enum
 * or list limit that differs, and on any schema keyword it does not know.
 */
import { describe, expect, it } from 'vitest'

import schema from '../../schema/portfolio.schema.json'
import { portfolioSchema } from './portfolio.ts'
import type { Node } from './schemaKit.ts'

type Json = Record<string, unknown>

const KNOWN = new Set([
  '$schema',
  '$defs',
  '$ref',
  'title',
  'description',
  'type',
  'properties',
  'required',
  'additionalProperties',
  'items',
  'maxItems',
  'uniqueItems',
  'anyOf',
  'oneOf',
  'enum',
  'const',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'pattern',
  'minLength',
  'maxLength',
])

const root = schema as unknown as Json

function resolve(node: Json): Json {
  let n = node
  while (typeof n.$ref === 'string') {
    const name = n.$ref.replace('#/$defs/', '')
    n = (root.$defs as Record<string, Json>)[name]!
    expect(n, `unresolved ${String(node.$ref)}`).toBeDefined()
  }
  return n
}

function compare(rawSchema: Json, desc: Node, path: string): void {
  const s = resolve(rawSchema)
  for (const key of Object.keys(s)) expect(KNOWN.has(key), `${path}: unknown schema keyword ${key}`).toBe(true)

  if (Array.isArray(s.anyOf)) {
    const options = (s.anyOf as Json[]).map(resolve)
    const nullOpt = options.find((o) => o.type === 'null')
    expect(nullOpt, `${path}: anyOf without a null branch`).toBeDefined()
    expect(options.length, `${path}: anyOf must be [null, X]`).toBe(2)
    expect(desc.kind, `${path}: schema is nullable`).toBe('nullable')
    if (desc.kind !== 'nullable') return
    compare(options.find((o) => o !== nullOpt)!, desc.inner, path)
    return
  }

  if (Array.isArray(s.oneOf)) {
    expect(desc.kind, `${path}: schema is a tagged union`).toBe('tagged')
    if (desc.kind !== 'tagged') return
    const seen: string[] = []
    for (const option of (s.oneOf as Json[]).map(resolve)) {
      const props = option.properties as Record<string, Json>
      const tag = (props[desc.key] as Json).const as string
      seen.push(tag)
      const variant = desc.variants[tag]
      expect(variant, `${path}: no variant for ${tag}`).toBeDefined()
      if (variant) compare(option, variant, `${path}<${tag}>`)
    }
    expect(Object.keys(desc.variants).sort()).toEqual(seen.sort())
    return
  }

  if ('const' in s) {
    expect(desc.kind, `${path}: const`).toBe('enum')
    if (desc.kind === 'enum') expect(desc.values).toEqual([s.const])
    return
  }

  if (Array.isArray(s.enum)) {
    expect(desc.kind, `${path}: enum`).toBe('enum')
    if (desc.kind === 'enum') expect([...desc.values].sort()).toEqual([...(s.enum as unknown[])].sort())
    return
  }

  switch (s.type) {
    case 'object': {
      expect(s.additionalProperties, `${path}: every schema object is closed`).toBe(false)
      expect(desc.kind, `${path}: object`).toBe('object')
      if (desc.kind !== 'object') return
      const props = s.properties as Record<string, Json>
      const required = new Set((s.required as string[] | undefined) ?? [])
      expect(Object.keys(desc.props).sort(), `${path}: property names`).toEqual(Object.keys(props).sort())
      for (const [key, child] of Object.entries(props)) {
        const d = desc.props[key] as Node
        if (required.has(key)) {
          expect(d.kind, `${path}.${key} is required`).not.toBe('optional')
          compare(child, d, `${path}.${key}`)
        } else {
          expect(d.kind, `${path}.${key} is optional`).toBe('optional')
          if (d.kind === 'optional') compare(child, d.inner, `${path}.${key}`)
        }
      }
      return
    }
    case 'array': {
      expect(desc.kind, `${path}: array`).toBe('array')
      if (desc.kind !== 'array') return
      expect(desc.maxItems, `${path}: maxItems`).toBe(s.maxItems)
      expect(desc.uniqueItems, `${path}: uniqueItems`).toBe(s.uniqueItems === true)
      compare(s.items as Json, desc.items, `${path}[]`)
      return
    }
    case 'number':
    case 'integer': {
      expect(desc.kind, `${path}: number`).toBe('number')
      if (desc.kind !== 'number') return
      expect(desc.integer, `${path}: integer`).toBe(s.type === 'integer')
      expect(desc.minimum, `${path}: minimum`).toBe(s.minimum)
      expect(desc.maximum, `${path}: maximum`).toBe(s.maximum)
      expect(desc.exclusiveMinimum, `${path}: exclusiveMinimum`).toBe(s.exclusiveMinimum)
      return
    }
    case 'string': {
      expect(desc.kind, `${path}: string`).toBe('string')
      if (desc.kind !== 'string') return
      expect(desc.pattern, `${path}: pattern`).toBe(s.pattern)
      expect(desc.minLength, `${path}: minLength`).toBe(s.minLength)
      expect(desc.maxLength, `${path}: maxLength`).toBe(s.maxLength)
      return
    }
    case 'boolean':
      expect(desc.kind, `${path}: boolean`).toBe('boolean')
      return
    default:
      throw new Error(`${path}: unhandled schema node`)
  }
}

describe('runtime check mirrors the JSON schema', () => {
  it('matches property for property, bound for bound', () => {
    compare(root, portfolioSchema, '$')
  })

  it('declares schema version 1', () => {
    expect(((root.properties as Json).meta as Json).properties).toMatchObject({ schema: { const: 1 } })
  })
})
