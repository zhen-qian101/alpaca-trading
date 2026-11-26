/**
 * vite.config.ts makes the SPA's build write the licences of every package it
 * bundles to the root of the static assets (dist/client/), where the Worker's
 * Static Assets serve it: not under Vite's default .vite/ directory, and not
 * for the Worker's own bundle, which never reaches a browser.
 */
import { fileURLToPath } from 'node:url'

import { loadConfigFromFile } from 'vite'
import { describe, expect, it } from 'vitest'

const configFile = fileURLToPath(new URL('../vite.config.ts', import.meta.url))

describe('vite.config.ts licences', () => {
  it('writes third-party-licenses.txt for the client build only', async () => {
    const loaded = await loadConfigFromFile({ command: 'build', mode: 'production' }, configFile, undefined, 'silent')
    expect(loaded).not.toBeNull()
    const config = loaded!.config
    expect(config.environments?.client?.build?.license).toEqual({ fileName: 'third-party-licenses.txt' })
    // Set per environment, never for every one (the Worker's included).
    expect(config.build?.license).toBeUndefined()
    const others = Object.entries(config.environments ?? {}).filter(([name]) => name !== 'client')
    for (const [name, env] of others) expect(env.build?.license, name).toBeUndefined()
  })
})
