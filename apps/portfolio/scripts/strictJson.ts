/**
 * Strict JSON reading for the deploy's gates (check-portfolio.ts, stage-data.ts).
 *
 * JSON.parse keeps the LAST of two equal keys. A file with `"data_delay"` in an
 * object twice therefore validates on the second copy while its bytes still
 * carry the first, and a gate that checks the parsed value but ships the
 * bytes lets the first copy out unseen. So the gates read a file with
 * parseStrictJson(), which refuses a duplicate key anywhere, and the deploy
 * ships canonicalJson() of the value that passed, never the downloaded bytes.
 * web/schema_gate.py `loads()` applies the same rule on the home box.
 *
 * Messages never quote the text: CI logs of a public repository are public.
 */

export class JsonTextError extends Error {
  override name = 'JsonTextError'
}

const utf8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })

/** Bytes to text; refuses bytes that are not UTF-8. A byte-order mark is kept, so JSON.parse refuses it. */
export function decodeUtf8(bytes: Uint8Array): string {
  try {
    return utf8.decode(bytes)
  } catch {
    throw new JsonTextError('not UTF-8 text')
  }
}

/**
 * Name separators (`:`) outside strings. In text JSON.parse accepted, each
 * one belongs to exactly one object member.
 */
function countMembers(text: string): number {
  let members = 0
  let inString = false
  let escaped = false
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (inString) {
      if (escaped) escaped = false
      else if (c === 0x5c) escaped = true // backslash
      else if (c === 0x22) inString = false // quote
    } else if (c === 0x22) inString = true
    else if (c === 0x3a) members++ // colon
  }
  return members
}

/** Own keys over every object in a parsed value (iterative: no recursion limit). */
function countKeys(value: unknown): number {
  let keys = 0
  const stack: unknown[] = [value]
  while (stack.length) {
    const v = stack.pop()
    if (Array.isArray(v)) for (const item of v) stack.push(item)
    else if (typeof v === 'object' && v !== null) {
      const own = Object.keys(v)
      keys += own.length
      for (const k of own) stack.push((v as Record<string, unknown>)[k])
    }
  }
  return keys
}

/**
 * JSON.parse, refusing a document in which any object has the same key
 * twice. Members are counted in the text and keys in the result: they differ
 * exactly when a duplicate was collapsed.
 */
export function parseStrictJson(text: string): unknown {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    // V8's message quotes the text around the error: never print it.
    throw new JsonTextError('not a JSON document')
  }
  if (countMembers(text) !== countKeys(value)) throw new JsonTextError('an object has the same key twice')
  return value
}

/** The one serialisation the deploy ships: of the validated value, never of the downloaded bytes. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(value)
}
