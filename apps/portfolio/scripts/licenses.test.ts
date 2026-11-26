/**
 * The built site ships the licences of what it bundles (src/lib/licenses.ts):
 * a real build writes them to the site root, the build completes what the
 * npm packages leave out, and scripts/check-licenses.ts refuses a file that
 * misses any of it.
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { build } from 'vite'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import { LICENSES_FILE, LICENSES_PATH, NOTICE_VERSION } from '../src/lib/licenses.ts'
import { bundledPackages, checkLicenseFile, completeLicenseFile, NOTICE_LINES, SUPPLEMENTS_HEADING } from './licenseNotices.ts'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) as { dependencies: Record<string, string> }
const installed = (name: string) =>
  (JSON.parse(readFileSync(join(root, 'node_modules', name, 'package.json'), 'utf8')) as { version: string }).version

describe('the built site', () => {
  let outDir = ''
  let text = ''

  beforeAll(async () => {
    outDir = mkdtempSync(join(tmpdir(), 'portfolio-build-'))
    vi.stubEnv('NODE_ENV', 'production')
    try {
      await build({
        root,
        configFile: join(root, 'vite.config.ts'),
        logLevel: 'silent',
        build: { outDir, emptyOutDir: true },
      })
    } finally {
      vi.unstubAllEnvs()
    }
    text = readFileSync(join(outDir, LICENSES_FILE), 'utf8')
  }, 120_000)

  afterAll(() => {
    if (outDir) rmSync(outDir, { recursive: true, force: true })
  })

  it('writes the licences file at the root, where the Worker serves it and the footer links', () => {
    expect(LICENSES_PATH).toBe(`/${LICENSES_FILE}`)
    expect(LICENSES_FILE).toMatch(/^[a-z][a-z-]*\.txt$/)
    expect(existsSync(join(outDir, 'index.html'))).toBe(true)
    expect(existsSync(join(outDir, '.vite'))).toBe(false)
  })

  it('lists React and Lightweight Charts with their licence texts', () => {
    const listed = bundledPackages(text)
    const entry = (name: string) => listed.find((p) => p.name === name)
    expect(entry('react')).toMatchObject({ identifier: 'MIT', hasText: true })
    expect(entry('react-dom')).toMatchObject({ identifier: 'MIT', hasText: true })
    expect(entry('lightweight-charts')).toMatchObject({ version: NOTICE_VERSION, identifier: 'Apache-2.0', hasText: true })
    expect(text).toContain('MIT License')
    expect(text).toContain('Apache License')
    expect(text).toContain('Version 2.0, January 2004')
  })

  it('adds the TradingView NOTICE and the licence fancy-canvas leaves out, and passes the deploy check', () => {
    expect(text).toContain(`\n${SUPPLEMENTS_HEADING}\n`)
    expect(text).toContain(NOTICE_LINES.join('\n'))
    expect(text).toContain('Copyright (c) 2019 TradingView, Inc\n')
    expect(checkLicenseFile(text, Object.keys(pkg.dependencies))).toEqual([])
  })
})

describe('the NOTICE the site quotes', () => {
  it('is from the installed lightweight-charts release', () => {
    expect(installed('lightweight-charts')).toBe(NOTICE_VERSION)
  })
})

describe('completing and checking the file', () => {
  const vite = (sections: string) => `# Licenses\n\nThe app bundles dependencies which contain the following licenses:\n${sections}`
  const LWC = `\n## lightweight-charts - ${NOTICE_VERSION} (Apache-2.0)\n\nApache License text\n`
  const REACT = '\n## react - 19.3.1 (MIT)\n\nMIT License text\n'

  it('appends the NOTICE once lightweight-charts is bundled', () => {
    const { text, problems } = completeLicenseFile(vite(REACT + LWC))
    expect(problems).toEqual([])
    expect(text).toContain(NOTICE_LINES.join('\n'))
    expect(checkLicenseFile(text, ['react', 'lightweight-charts'])).toEqual([])
  })

  it('fails the build for a package with no licence text, or a supplement for another version', () => {
    expect(completeLicenseFile(vite(`${REACT}\n## left-pad - 1.3.0 (WTFPL)\n`)).problems).toEqual([
      'left-pad 1.3.0 ships no licence file: add its licence text to scripts/licenseNotices.ts',
    ])
    const newer = completeLicenseFile(vite(LWC.replace(NOTICE_VERSION, '9.9.9'))).problems
    expect(newer).toHaveLength(1)
    expect(newer[0]).toMatch(/^lightweight-charts 9\.9\.9 is bundled, but the text supplied for it is from /)
    expect(completeLicenseFile(vite('')).problems).toEqual(['the licences file lists no bundled package'])
  })

  it('refuses a file without a dependency, a licence text or the NOTICE', () => {
    expect(checkLicenseFile(vite(REACT + LWC), ['react', 'react-dom', 'lightweight-charts'])).toEqual([
      'react-dom is not listed',
      'the TradingView NOTICE is missing',
    ])
    expect(checkLicenseFile(vite('\n## fancy-canvas - 2.1.0 (MIT)\n'), [])).toEqual([
      'fancy-canvas 2.1.0 is listed without a licence text',
    ])
  })
})
