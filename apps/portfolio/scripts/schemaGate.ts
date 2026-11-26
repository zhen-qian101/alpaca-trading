/**
 * A dependency-free JSON Schema validator: the deploy's leak gate.
 *
 * The deploy job (.github/workflows/portfolio.yml) downloads portfolio.json
 * from R2 and runs it through this before anything is staged or deployed. It
 * is the Node twin of web/schema_gate.py, which gates the same file on the
 * home box before upload, and it implements the same subset of JSON Schema
 * 2020-12 and nothing else, ON PURPOSE:
 *
 *  - checkSchema() REFUSES a schema that uses any keyword outside that subset.
 *    A keyword this validator silently ignored (say `format`, or a typo such
 *    as `additionalProperty`) would make the gate look stricter than it is.
 *  - checkSchema() also refuses an object schema that does not set
 *    `additionalProperties: false`, so an undeclared field can never pass
 *    because one nested object forgot the line.
 *  - Problems are reported as a JSON pointer (RFC 6901) and the broken rule,
 *    never the value. The pointer is built from schema-declared property names
 *    and array indexes only: an undeclared key is reported at its parent, since
 *    the key itself is data. CI logs of a public repository are public.
 *
 * No third-party package, so the gate runs in CI before `pnpm install` could
 * matter, and nothing here can be swapped by a dependency update.
 */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
type SchemaNode = Record<string, unknown>

/** Keywords that carry no validation (annotations and the $defs container). */
const ANNOTATIONS = new Set(['$schema', '$id', '$comment', 'title', 'description', 'examples', '$defs'])
/** Keywords this validator implements. */
const ASSERTIONS = new Set([
  '$ref',
  'type',
  'enum',
  'const',
  'properties',
  'required',
  'additionalProperties',
  'items',
  'minItems',
  'maxItems',
  'uniqueItems',
  'pattern',
  'minLength',
  'maxLength',
  'minimum',
  'maximum',
  'exclusiveMinimum',
  'exclusiveMaximum',
  'anyOf',
  'oneOf',
])
const TYPES = new Set(['null', 'boolean', 'integer', 'number', 'string', 'array', 'object'])
const REF_PREFIX = '#/$defs/'
export const MAX_PROBLEMS = 50

export class SchemaError extends Error {
  override name = 'SchemaError'
}

export interface Problem {
  /** RFC 6901 pointer into the instance; "" is the document root. */
  readonly pointer: string
  readonly rule: string
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

function typesOf(node: SchemaNode): Set<string> {
  const t = node.type
  if (t === undefined) return new Set()
  if (typeof t === 'string') return new Set([t])
  if (Array.isArray(t)) return new Set(t.map(String))
  return new Set(['?'])
}

function compile(pattern: unknown, where: string): RegExp {
  if (typeof pattern !== 'string') throw new SchemaError(`${where}: pattern is not a string`)
  try {
    return new RegExp(pattern, 'u')
  } catch {
    throw new SchemaError(`${where}: pattern does not compile`)
  }
}

/**
 * Throw SchemaError unless `schema` uses only supported keywords, every $ref
 * resolves, every pattern compiles, and every object schema closes itself
 * with additionalProperties: false.
 */
export function checkSchema(schema: unknown): asserts schema is SchemaNode {
  if (!isObject(schema)) throw new SchemaError('the schema is not a JSON object')
  const defs = schema.$defs ?? {}
  if (!isObject(defs)) throw new SchemaError('$defs is not an object')

  const walk = (node: unknown, where: string): void => {
    if (!isObject(node)) throw new SchemaError(`${where}: a subschema must be an object`)
    const unknown = Object.keys(node).filter((k) => !ANNOTATIONS.has(k) && !ASSERTIONS.has(k))
    if (unknown.length) throw new SchemaError(`${where}: unsupported keyword(s) ${unknown.sort().join(', ')}`)
    const types = typesOf(node)
    for (const t of types) if (!TYPES.has(t)) throw new SchemaError(`${where}: unknown type`)
    if ((types.has('object') || 'properties' in node || 'required' in node) && node.additionalProperties !== false)
      throw new SchemaError(`${where}: an object schema must set additionalProperties: false`)
    if ('additionalProperties' in node && node.additionalProperties !== false)
      throw new SchemaError(`${where}: additionalProperties must be false`)
    if ('$ref' in node) {
      const ref = node.$ref
      if (!(typeof ref === 'string' && ref.startsWith(REF_PREFIX) && Object.hasOwn(defs, ref.slice(REF_PREFIX.length))))
        throw new SchemaError(`${where}: $ref does not name an entry of $defs`)
    }
    if ('pattern' in node) compile(node.pattern, where)
    const props = node.properties ?? {}
    if (!isObject(props)) throw new SchemaError(`${where}: properties is not an object`)
    if ('required' in node) {
      const req = node.required
      if (!(Array.isArray(req) && req.every((n) => typeof n === 'string' && Object.hasOwn(props, n))))
        throw new SchemaError(`${where}: required names a property that is not declared`)
    }
    if ('enum' in node && !(Array.isArray(node.enum) && node.enum.length))
      throw new SchemaError(`${where}: enum must be a non-empty list`)
    for (const key of ['minItems', 'maxItems', 'minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'])
      if (key in node && !(typeof node[key] === 'number' && Number.isFinite(node[key])))
        throw new SchemaError(`${where}: ${key} must be a finite number`)
    if ('uniqueItems' in node && typeof node.uniqueItems !== 'boolean')
      throw new SchemaError(`${where}: uniqueItems must be true or false`)
    for (const [name, sub] of Object.entries(props)) walk(sub, `${where}.properties.${name}`)
    if ('items' in node) walk(node.items, `${where}.items`)
    for (const key of ['anyOf', 'oneOf'] as const) {
      if (!(key in node)) continue
      const branches = node[key]
      if (!(Array.isArray(branches) && branches.length)) throw new SchemaError(`${where}: ${key} must be a non-empty list`)
      branches.forEach((sub, i) => walk(sub, `${where}.${key}[${i}]`))
    }
  }

  walk(schema, '#')
  for (const [name, sub] of Object.entries(defs)) walk(sub, `#/$defs/${name}`)
}

// ------------------------------------------------------------------ instances

const isNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function typeOk(v: unknown, t: string): boolean {
  switch (t) {
    case 'null':
      return v === null
    case 'boolean':
      return typeof v === 'boolean'
    case 'integer':
      return isNumber(v) && Number.isInteger(v)
    case 'number':
      return isNumber(v)
    case 'string':
      return typeof v === 'string'
    case 'array':
      return Array.isArray(v)
    case 'object':
      return isObject(v)
    default:
      return false
  }
}

/** A key that compares the way JSON Schema compares: true != 1, 1 == 1.0, key order ignored. */
function canon(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`
  if (isObject(v))
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canon(v[k])}`)
      .join(',')}}`
  return JSON.stringify(v) ?? 'undefined'
}

const escapeToken = (token: string) => token.replaceAll('~', '~0').replaceAll('/', '~1')

/** Every rule `instance` breaks (at most MAX_PROBLEMS). Empty means valid. */
export function validate(instance: unknown, schema: SchemaNode): Problem[] {
  const problems: Problem[] = []
  const patterns = new Map<string, RegExp>()
  const defs = (schema.$defs ?? {}) as Record<string, SchemaNode>

  const check = (v: unknown, node: SchemaNode, pointer: string, out: Problem[]): void => {
    if (out.length >= MAX_PROBLEMS) return
    const fail = (rule: string) => {
      out.push({ pointer, rule })
    }
    if (typeof node.$ref === 'string') check(v, defs[node.$ref.slice(REF_PREFIX.length)]!, pointer, out)

    const types = typesOf(node)
    if (types.size && ![...types].some((t) => typeOk(v, t))) {
      fail(`expected ${[...types].sort().join('/')}`)
      return
    }
    if ('const' in node && canon(v) !== canon(node.const)) fail('not the required constant')
    if (Array.isArray(node.enum) && !node.enum.some((e) => canon(e) === canon(v))) fail('not one of the allowed values')

    if (typeof v === 'number') {
      if (!Number.isFinite(v)) fail('not a finite number')
      else {
        if (typeof node.minimum === 'number' && v < node.minimum) fail('below the minimum')
        if (typeof node.maximum === 'number' && v > node.maximum) fail('above the maximum')
        if (typeof node.exclusiveMinimum === 'number' && v <= node.exclusiveMinimum) fail('not above the exclusive minimum')
        if (typeof node.exclusiveMaximum === 'number' && v >= node.exclusiveMaximum) fail('not below the exclusive maximum')
      }
    }

    if (typeof v === 'string') {
      // JSON Schema counts code points, not UTF-16 units.
      const length = [...v].length
      if (typeof node.minLength === 'number' && length < node.minLength) fail('shorter than minLength')
      if (typeof node.maxLength === 'number' && length > node.maxLength) fail('longer than maxLength')
      if (typeof node.pattern === 'string') {
        let re = patterns.get(node.pattern)
        if (!re) patterns.set(node.pattern, (re = compile(node.pattern, pointer)))
        if (!re.test(v)) fail('does not match the pattern')
      }
    }

    if (Array.isArray(v)) {
      if (typeof node.minItems === 'number' && v.length < node.minItems) fail('fewer items than minItems')
      if (typeof node.maxItems === 'number' && v.length > node.maxItems) fail('more items than maxItems')
      if (node.uniqueItems === true && new Set(v.map(canon)).size !== v.length) fail('items are not unique')
      if (isObject(node.items)) {
        const items = node.items
        v.forEach((item, i) => check(item, items, `${pointer}/${i}`, out))
      }
    }

    if (isObject(v)) {
      const props = (node.properties ?? {}) as Record<string, SchemaNode>
      for (const name of (node.required ?? []) as string[])
        if (!Object.hasOwn(v, name)) fail(`missing required property "${name}"`)
      if (node.additionalProperties === false && Object.keys(v).some((k) => !Object.hasOwn(props, k)))
        // The key is data: name the object that holds it, never the key.
        fail('has a property the schema does not declare')
      for (const [name, sub] of Object.entries(props))
        if (Object.hasOwn(v, name)) check(v[name], sub, `${pointer}/${escapeToken(name)}`, out)
    }

    if (Array.isArray(node.anyOf)) {
      const branches = (node.anyOf as SchemaNode[]).map((s) => subProblems(v, s, pointer))
      if (branches.every((b) => b.length)) {
        fail('matches none of anyOf')
        // The closest branch says what is actually wrong (for a nullable
        // field, that is the non-null branch).
        out.push(...closest(branches, pointer))
      }
    }
    if (Array.isArray(node.oneOf)) {
      const matched = (node.oneOf as SchemaNode[]).filter((s) => !subProblems(v, s, pointer).length).length
      if (matched !== 1) fail(`matches ${matched} of oneOf (exactly 1 required)`)
    }
  }

  const subProblems = (v: unknown, node: SchemaNode, pointer: string): Problem[] => {
    const out: Problem[] = []
    check(v, node, pointer, out)
    return out
  }

  /** The failing branch worth reporting: one whose type matched, then the one with the fewest problems. */
  const closest = (branches: Problem[][], pointer: string): Problem[] => {
    const typeOnly = (b: Problem[]) => b.length === 1 && b[0]!.pointer === pointer && b[0]!.rule.startsWith('expected ')
    return [...branches].sort((a, b) => Number(typeOnly(a)) - Number(typeOnly(b)) || a.length - b.length)[0]!
  }

  check(instance, schema, '', problems)
  return problems.slice(0, MAX_PROBLEMS)
}

/** How a problem is printed: pointer and rule, never a value. */
export const formatProblem = (p: Problem) => `${p.pointer === '' ? '(root)' : p.pointer}: ${p.rule}`
