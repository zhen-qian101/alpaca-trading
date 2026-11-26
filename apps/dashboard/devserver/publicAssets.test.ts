/**
 * Every icon, manifest and font the page or the web app manifest names must
 * exist in public/ (Vite copies it into the build as-is). A missing one does
 * not even 404 once deployed: `not_found_handling: "single-page-application"`
 * answers it with index.html, so the install prompt and home-screen icon break
 * without an error anywhere. PNG icons must also be the size they claim.
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const APP = join(dirname(fileURLToPath(import.meta.url)), '..')
const PUBLIC = join(APP, 'public')

interface ManifestIcon {
  src: string
  sizes?: string
  type?: string
  purpose?: string
}

interface Manifest {
  icons: ManifestIcon[]
  shortcuts?: { icons?: ManifestIcon[] }[]
}

const manifest = JSON.parse(readFileSync(join(PUBLIC, 'manifest.webmanifest'), 'utf8')) as Manifest
const indexHtml = readFileSync(join(APP, 'index.html'), 'utf8')

/** Root-relative href/src values of <link> elements in index.html. */
function linkedFromIndex(): string[] {
  return [...indexHtml.matchAll(/<link\b[^>]*\bhref="(\/[^"]*)"/g)].map((m) => m[1])
}

function allManifestIcons(): ManifestIcon[] {
  return [...manifest.icons, ...(manifest.shortcuts ?? []).flatMap((s) => s.icons ?? [])]
}

function pngSize(path: string): [number, number] {
  const buf = readFileSync(path)
  expect(buf.subarray(1, 4).toString('latin1')).toBe('PNG')
  return [buf.readUInt32BE(16), buf.readUInt32BE(20)]
}

describe('public assets', () => {
  it('has every file index.html links to', () => {
    const hrefs = linkedFromIndex()
    expect(hrefs).toContain('/manifest.webmanifest')
    expect(hrefs).toContain('/icons/apple-touch-icon.png')
    for (const href of hrefs) expect(existsSync(join(PUBLIC, href)), href).toBe(true)
  })

  it('has every icon the manifest names, at the size it declares', () => {
    const icons = allManifestIcons()
    expect(icons.length).toBeGreaterThan(0)
    for (const icon of icons) {
      const file = join(PUBLIC, icon.src)
      expect(existsSync(file), icon.src).toBe(true)
      if (icon.src.endsWith('.png')) {
        const [w, h] = pngSize(file)
        expect(`${w}x${h}`, icon.src).toBe(icon.sizes)
      }
    }
  })

  it('offers an installable 192 and 512 icon and a maskable one', () => {
    const png = manifest.icons.filter((i) => i.type === 'image/png')
    expect(png.some((i) => i.sizes === '192x192' && i.purpose !== 'maskable')).toBe(true)
    expect(png.some((i) => i.sizes === '512x512' && i.purpose !== 'maskable')).toBe(true)
    expect(png.some((i) => i.purpose === 'maskable')).toBe(true)
  })

  it('has a 180x180 apple-touch-icon', () => {
    expect(pngSize(join(PUBLIC, 'icons/apple-touch-icon.png'))).toEqual([180, 180])
  })
})
