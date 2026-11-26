/**
 * A very small runtime validator for CLOSED JSON shapes.
 *
 * The public portfolio file is gated by apps/portfolio/schema/portfolio.schema.json
 * before it is uploaded. The site checks it again when it loads, with these
 * descriptors, because a page that renders whatever it is handed would show
 * a field nobody whitelisted the moment the exporter and the schema drifted.
 *
 * Every object is closed: a key the descriptor does not declare is an error.
 * The TypeScript types are inferred from the same descriptors (`Infer`), so
 * the types the pages use cannot disagree with what was checked.
 * src/data/schemaSync.test.ts walks the JSON schema next to these descriptors
 * and fails if any property, bound, pattern or enum differs.
 *
 * Error messages name the JSON path and the broken rule, never the value.
 */

export interface NumNode {
  readonly kind: 'number'
  readonly integer: boolean
  readonly minimum?: number
  readonly maximum?: number
  readonly exclusiveMinimum?: number
}

export interface StrNode {
  readonly kind: 'string'
  readonly pattern?: string
  readonly re?: RegExp
  readonly minLength?: number
  readonly maxLength?: number
}

export interface BoolNode {
  readonly kind: 'boolean'
}

export interface EnumNode<T extends string | number = string | number> {
  readonly kind: 'enum'
  readonly values: readonly T[]
}

export interface ArrNode<I extends Node = Node> {
  readonly kind: 'array'
  readonly items: I
  readonly maxItems: number
  readonly uniqueItems: boolean
}

export interface NullableNode<I extends Node = Node> {
  readonly kind: 'nullable'
  readonly inner: I
}

/** Only meaningful as an object property: the key may be absent. */
export interface OptionalNode<I extends Node = Node> {
  readonly kind: 'optional'
  readonly inner: I
}

export type Props = { readonly [key: string]: Node }

export interface ObjNode<P extends Props = Props> {
  readonly kind: 'object'
  readonly props: P
}

/** A closed union of objects told apart by one constant property. */
export interface TaggedNode<V extends { readonly [tag: string]: ObjNode } = { readonly [tag: string]: ObjNode }> {
  readonly kind: 'tagged'
  readonly key: string
  readonly variants: V
}

export type Node =
  | NumNode
  | StrNode
  | BoolNode
  | EnumNode
  | ArrNode
  | NullableNode
  | OptionalNode
  | ObjNode
  | TaggedNode

type Simplify<T> = { [K in keyof T]: T[K] } & {}

type OptionalKeys<P> = { [K in keyof P]: P[K] extends { kind: 'optional' } ? K : never }[keyof P]

type InferObject<P> = Simplify<
  { readonly [K in Exclude<keyof P, OptionalKeys<P>>]: Infer<P[K]> } & {
    readonly [K in OptionalKeys<P>]?: P[K] extends OptionalNode<infer I> ? Infer<I> : never
  }
>

/** The TypeScript type a descriptor accepts. */
export type Infer<N> = N extends { kind: 'number' }
  ? number
  : N extends { kind: 'string' }
    ? string
    : N extends { kind: 'boolean' }
      ? boolean
      : N extends EnumNode<infer T>
        ? T
        : N extends ArrNode<infer I>
          ? readonly Infer<I>[]
          : N extends NullableNode<infer I>
            ? Infer<I> | null
            : N extends ObjNode<infer P>
              ? InferObject<P>
              : N extends TaggedNode<infer V>
                ? { [K in keyof V]: Infer<V[K]> }[keyof V]
                : never

// ------------------------------------------------------------------ builders

export const num = (o: { minimum?: number; maximum?: number; exclusiveMinimum?: number } = {}): NumNode => ({
  kind: 'number',
  integer: false,
  ...o,
})

export const int = (o: { minimum?: number; maximum?: number } = {}): NumNode => ({
  kind: 'number',
  integer: true,
  ...o,
})

export const str = (o: { pattern?: string; minLength?: number; maxLength?: number } = {}): StrNode => ({
  kind: 'string',
  ...o,
  ...(o.pattern === undefined ? {} : { re: new RegExp(o.pattern, 'u') }),
})

export const bool = (): BoolNode => ({ kind: 'boolean' })

export const oneOf = <const T extends string | number>(...values: T[]): EnumNode<T> => ({ kind: 'enum', values })

export const arr = <I extends Node>(items: I, maxItems: number, o: { uniqueItems?: boolean } = {}): ArrNode<I> => ({
  kind: 'array',
  items,
  maxItems,
  uniqueItems: o.uniqueItems ?? false,
})

export const nullable = <I extends Node>(inner: I): NullableNode<I> => ({ kind: 'nullable', inner })

export const optional = <I extends Node>(inner: I): OptionalNode<I> => ({ kind: 'optional', inner })

export const obj = <const P extends Props>(props: P): ObjNode<P> => ({ kind: 'object', props })

export const tagged = <const V extends { readonly [tag: string]: ObjNode }>(key: string, variants: V): TaggedNode<V> => ({
  kind: 'tagged',
  key,
  variants,
})

// ---------------------------------------------------------------- validation

const MAX_ERRORS = 25

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return false
  const proto = Object.getPrototypeOf(v) as unknown
  return proto === Object.prototype || proto === null
}

function check(node: Node, value: unknown, path: string, errors: string[]): boolean {
  if (errors.length >= MAX_ERRORS) return false
  const fail = (rule: string) => {
    errors.push(`${path}: ${rule}`)
    return false
  }

  switch (node.kind) {
    case 'number': {
      if (typeof value !== 'number' || !Number.isFinite(value)) return fail('expected a finite number')
      if (node.integer && !Number.isInteger(value)) return fail('expected an integer')
      if (node.minimum !== undefined && value < node.minimum) return fail(`below minimum ${node.minimum}`)
      if (node.maximum !== undefined && value > node.maximum) return fail(`above maximum ${node.maximum}`)
      if (node.exclusiveMinimum !== undefined && value <= node.exclusiveMinimum)
        return fail(`not above ${node.exclusiveMinimum}`)
      return true
    }
    case 'string': {
      if (typeof value !== 'string') return fail('expected a string')
      // JSON Schema counts code points, not UTF-16 units.
      const length = [...value].length
      if (node.minLength !== undefined && length < node.minLength) return fail(`shorter than ${node.minLength}`)
      if (node.maxLength !== undefined && length > node.maxLength) return fail(`longer than ${node.maxLength}`)
      if (node.re && !node.re.test(value)) return fail('does not match the required pattern')
      return true
    }
    case 'boolean':
      return typeof value === 'boolean' ? true : fail('expected true or false')
    case 'enum':
      return (node.values as readonly unknown[]).includes(value) ? true : fail('not one of the allowed values')
    case 'nullable':
      return value === null ? true : check(node.inner, value, path, errors)
    case 'optional':
      return check(node.inner, value, path, errors)
    case 'array': {
      if (!Array.isArray(value)) return fail('expected an array')
      if (value.length > node.maxItems) return fail(`more than ${node.maxItems} items`)
      let ok = true
      value.forEach((item, i) => {
        ok = check(node.items, item, `${path}[${i}]`, errors) && ok
      })
      if (node.uniqueItems && new Set(value.map((v) => JSON.stringify(v))).size !== value.length)
        return fail('items are not unique')
      return ok
    }
    case 'object': {
      if (!isPlainObject(value)) return fail('expected an object')
      let ok = true
      for (const key of Object.keys(value)) {
        if (!Object.hasOwn(node.props, key)) {
          // Name the path, not the key: a key is data too.
          ok = fail('has a field the schema does not allow')
        }
      }
      for (const [key, child] of Object.entries(node.props) as [string, Node][]) {
        if (!Object.hasOwn(value, key)) {
          if (child.kind !== 'optional') ok = fail(`missing required field "${key}"`)
          continue
        }
        ok = check(child, value[key], `${path}.${key}`, errors) && ok
      }
      return ok
    }
    case 'tagged': {
      if (!isPlainObject(value)) return fail('expected an object')
      const tag = value[node.key]
      const variant = typeof tag === 'string' && Object.hasOwn(node.variants, tag) ? node.variants[tag] : undefined
      if (!variant) return fail(`unknown "${node.key}"`)
      return check(variant, value, path, errors)
    }
  }
}

export type Validation<T> = { ok: true; value: T } | { ok: false; errors: string[] }

/** Validate `value` against `node`; on success the value is returned typed. */
export function validate<N extends Node>(node: N, value: unknown): Validation<Infer<N>> {
  const errors: string[] = []
  const ok = check(node, value, '$', errors)
  return ok && errors.length === 0 ? { ok: true, value: value as Infer<N> } : { ok: false, errors }
}
