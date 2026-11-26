/**
 * Completes and checks the licences file the build writes
 * (src/lib/licenses.ts; vite.config.ts `build.license`).
 *
 * Vite lists every package bundled into the site, each with the licence file
 * it finds in that package. Two of them leave something out, so the build
 * appends it:
 *
 * - lightweight-charts (Apache-2.0) ships its LICENSE but not its NOTICE.
 * - fancy-canvas, lightweight-charts' one dependency, is MIT by its
 *   package.json but ships no licence text, and MIT asks that the copyright
 *   and permission notice go with every copy.
 *
 * Each supplement names the version it was copied from. The build fails when
 * the bundled version differs, so an upgrade re-reads the upstream file instead
 * of shipping a stale one, and when a bundled package has neither a licence
 * text of its own nor a supplement.
 *
 * Dependency-free, like the other scripts here (scripts/check-licenses.ts runs
 * it under plain Node on a built dist/).
 */
import { NOTICE_VERSION, TRADINGVIEW_NOTICE } from '../src/lib/licenses.ts'

/** One package as Vite's licence file lists it. */
export interface BundledPackage {
  name: string
  version: string
  identifier: string | null
  /** The package carried a licence file, and its text is in the file. */
  hasText: boolean
}

interface Supplement {
  name: string
  version: string
  heading: string
  body: string
}

/** Everything after this line is appended by the build, not written by Vite. */
export const SUPPLEMENTS_HEADING = '# Notices and licence texts the packages leave out'

const FANCY_CANVAS_VERSION = '2.1.0'

/** https://github.com/tradingview/fancy-canvas/blob/2.1.0/LICENSE, verbatim. */
const FANCY_CANVAS_LICENSE = `Copyright (c) 2019 TradingView, Inc

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.`

/** The NOTICE lines as the licences file carries them. */
export const NOTICE_LINES = [TRADINGVIEW_NOTICE.product, `${TRADINGVIEW_NOTICE.copyright} ${TRADINGVIEW_NOTICE.url}`]

const SUPPLEMENTS: Supplement[] = [
  {
    name: 'lightweight-charts',
    version: NOTICE_VERSION,
    heading: `## NOTICE of lightweight-charts ${NOTICE_VERSION}`,
    body: [
      `From https://github.com/tradingview/lightweight-charts/blob/v${NOTICE_VERSION}/NOTICE`,
      '(the npm package does not include it):',
      '',
      ...NOTICE_LINES,
    ].join('\n'),
  },
  {
    name: 'fancy-canvas',
    version: FANCY_CANVAS_VERSION,
    heading: `## Licence of fancy-canvas ${FANCY_CANVAS_VERSION} (MIT)`,
    body: [
      `From https://github.com/tradingview/fancy-canvas/blob/${FANCY_CANVAS_VERSION}/LICENSE`,
      '(the npm package does not include it):',
      '',
      FANCY_CANVAS_LICENSE,
    ].join('\n'),
  },
]

/** Where each supplement's version is kept, for the error message. */
const KEPT_IN: Record<string, string> = {
  'lightweight-charts': 'NOTICE_VERSION and TRADINGVIEW_NOTICE in src/lib/licenses.ts',
  'fancy-canvas': 'FANCY_CANVAS_LICENSE in scripts/licenseNotices.ts',
}

/** The packages Vite listed, read from the part of the file Vite wrote. */
export function bundledPackages(text: string): BundledPackage[] {
  const at = text.indexOf(`\n${SUPPLEMENTS_HEADING}\n`)
  const own = at < 0 ? text : text.slice(0, at)
  const headings = [...own.matchAll(/^## (\S+) - (\S+)(?: \((.*)\))?$/gm)]
  return headings.map((m, i) => {
    const end = i + 1 < headings.length ? headings[i + 1]!.index : own.length
    return {
      name: m[1]!,
      version: m[2]!,
      identifier: m[3] ?? null,
      hasText: own.slice(m.index + m[0].length, end).trim().length > 0,
    }
  })
}

/**
 * Vite's licence file plus the supplements for the packages it lists, and the
 * problems that must fail the build.
 */
export function completeLicenseFile(viteText: string): { text: string; problems: string[] } {
  const bundled = bundledPackages(viteText)
  const problems: string[] = []
  if (bundled.length === 0) problems.push('the licences file lists no bundled package')
  const used: Supplement[] = []
  for (const pkg of bundled) {
    const supplement = SUPPLEMENTS.find((s) => s.name === pkg.name)
    if (supplement) {
      if (supplement.version !== pkg.version)
        problems.push(
          `${pkg.name} ${pkg.version} is bundled, but the text supplied for it is from ${supplement.version}: ` +
            `re-read that release's upstream file and update ${KEPT_IN[pkg.name]}`,
        )
      used.push(supplement)
    } else if (!pkg.hasText) {
      problems.push(`${pkg.name} ${pkg.version} ships no licence file: add its licence text to scripts/licenseNotices.ts`)
    }
  }
  if (used.length === 0) return { text: viteText, problems }
  const appended = used.map((s) => `${s.heading}\n\n${s.body}\n`).join('\n')
  return { text: `${viteText.trimEnd()}\n\n${SUPPLEMENTS_HEADING}\n\n${appended}`, problems }
}

/**
 * Problems with a finished licences file: every name in `required` listed,
 * every listed package with a licence text (its own or a supplement), and
 * the TradingView NOTICE present once lightweight-charts is bundled.
 */
export function checkLicenseFile(text: string, required: string[]): string[] {
  const bundled = bundledPackages(text)
  const problems: string[] = []
  for (const name of required)
    if (!bundled.some((p) => p.name === name)) problems.push(`${name} is not listed`)
  for (const pkg of bundled) {
    const supplement = SUPPLEMENTS.find((s) => s.name === pkg.name && s.version === pkg.version)
    const supplied = supplement !== undefined && text.includes(`\n${supplement.heading}\n`)
    if (!pkg.hasText && !supplied) problems.push(`${pkg.name} ${pkg.version} is listed without a licence text`)
  }
  if (bundled.some((p) => p.name === 'lightweight-charts') && !text.includes(NOTICE_LINES.join('\n')))
    problems.push('the TradingView NOTICE is missing')
  return problems
}
