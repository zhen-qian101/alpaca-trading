/**
 * Gate a portfolio.json with schema/portfolio.schema.json. The deploy job runs
 * this on the file it downloaded from R2, before anything is staged or
 * deployed, and fails closed:
 *
 *   node scripts/check-portfolio.ts FILE [--schema PATH]
 *
 *   exit 0  the file is valid
 *   exit 1  the file is refused: unreadable, too large, not UTF-8 JSON, an
 *           object with the same key twice (scripts/strictJson.ts), or invalid
 *   exit 2  usage error, or the schema itself is missing, unreadable or not
 *           strict enough to gate anything (scripts/schemaGate.ts)
 *
 * Output is pass/fail, counts of problems and, per problem, the JSON pointer
 * and the rule. Never a value, never the file, and never the JSON parser's
 * message (V8 quotes the text around a syntax error).
 *
 * This gate passes a VALUE; what ships is stage-data's re-serialisation of
 * that value, never these bytes.
 */
import { readFileSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { checkSchema, formatProblem, MAX_PROBLEMS, validate } from './schemaGate.ts'
import { decodeUtf8, JsonTextError, parseStrictJson } from './strictJson.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DEFAULT_SCHEMA = resolve(root, 'schema', 'portfolio.schema.json')
/** Far above a real export (a few hundred KB at the schema's list limits). */
const MAX_BYTES = 4 * 1024 * 1024

function main(argv: string[]): number {
  let schemaPath = DEFAULT_SCHEMA
  const files: string[] = []
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--schema') schemaPath = argv[++i] ?? ''
    else files.push(argv[i]!)
  }
  if (files.length !== 1 || !schemaPath) {
    console.error('usage: check-portfolio FILE [--schema PATH]')
    return 2
  }

  let schema: unknown
  try {
    schema = parseStrictJson(decodeUtf8(readFileSync(schemaPath)))
    checkSchema(schema)
  } catch (err) {
    const why =
      err instanceof Error && err.name === 'SchemaError'
        ? err.message
        : err instanceof JsonTextError && err.message.includes('same key')
          ? err.message
          : 'cannot read it as JSON'
    console.error(`schema unusable (${why}): nothing is published without it`)
    return 2
  }

  let data: unknown
  try {
    const size = statSync(files[0]!).size
    if (size > MAX_BYTES) {
      console.error(`refused: the file is larger than ${MAX_BYTES} bytes`)
      return 1
    }
    data = parseStrictJson(decodeUtf8(readFileSync(files[0]!)))
  } catch (err) {
    // A duplicate key is named as such (the key itself is data and is not);
    // anything else is "could not be read".
    if (err instanceof JsonTextError && err.message.includes('same key'))
      console.error('refused: an object in the file has the same key twice')
    else console.error('refused: the file could not be read as JSON')
    return 1
  }

  const problems = validate(data, schema)
  if (problems.length) {
    const more = problems.length >= MAX_PROBLEMS ? ' or more' : ''
    console.error(`refused: ${problems.length}${more} problem(s) against the schema`)
    for (const p of problems) console.error(`  ${formatProblem(p)}`)
    return 1
  }
  console.log('valid: the file passes the schema gate')
  return 0
}

process.exitCode = main(process.argv.slice(2))
