import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { createServer, preview, type Plugin, type PreviewServer, type Rolldown, type ViteDevServer } from 'vite'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { DEV_PEER_HEADER, DEV_PEER_KEY_HEADER, DEV_PEER_KEY_MIN_LENGTH, DEV_PEER_KEY_VAR } from '../worker/devPeer.ts'
import { devPeerGuard, stampPeer, stripBypassFromBundle, workerVarsFor } from './peerStamp.ts'

const PEER = DEV_PEER_HEADER.toLowerCase()
const KEY = DEV_PEER_KEY_HEADER.toLowerCase()
const TEST_KEY = 'f'.repeat(64)

function fakeReq(headers: Record<string, string | string[]>, remoteAddress: string | undefined): IncomingMessage {
  return { headers: { ...headers }, socket: { remoteAddress } } as unknown as IncomingMessage
}

describe('stampPeer', () => {
  it('replaces client-sent values with the connection peer and the key', () => {
    const req = fakeReq({ [PEER]: '127.0.0.1', [KEY]: 'guessed', host: 'localhost:5173' }, '192.168.1.20')
    let called = 0
    stampPeer(TEST_KEY)(req, {} as ServerResponse, () => called++)
    expect(req.headers[PEER]).toBe('192.168.1.20')
    expect(req.headers[KEY]).toBe(TEST_KEY)
    expect(called).toBe(1)
  })

  it('stamps a request that sent nothing, and an unknown peer as empty', () => {
    const req = fakeReq({}, '::1')
    stampPeer(TEST_KEY)(req, {} as ServerResponse, () => {})
    expect(req.headers[PEER]).toBe('::1')
    const gone = fakeReq({ [PEER]: ['127.0.0.1', '127.0.0.1'] }, undefined)
    stampPeer(TEST_KEY)(gone, {} as ServerResponse, () => {})
    expect(gone.headers[PEER]).toBe('')
  })
})

describe('devPeerGuard', () => {
  it('makes a fresh, long key per dev server process', () => {
    const a = devPeerGuard()
    const b = devPeerGuard()
    expect(a.key).toMatch(/^[0-9a-f]{64}$/)
    expect(a.key.length).toBeGreaterThanOrEqual(DEV_PEER_KEY_MIN_LENGTH)
    expect(a.key).not.toBe(b.key)
  })

  it('hands the key to the Worker under vite dev only', () => {
    const guard = devPeerGuard()
    expect(workerVarsFor({ command: 'serve', mode: 'development', isPreview: false }, guard)).toEqual({
      [DEV_PEER_KEY_VAR]: guard.key,
    })
    // A build would write vars into the deployable wrangler.json; preview serves that build.
    expect(workerVarsFor({ command: 'build', mode: 'production' }, guard)).toBeUndefined()
    expect(workerVarsFor({ command: 'serve', mode: 'production', isPreview: true }, guard)).toBeUndefined()
  })
})

/** Answers /echo with the headers exactly as a later middleware (the Cloudflare plugin) receives them. */
function echo(): Plugin {
  const handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (req.url !== '/echo') return next()
    res.setHeader('content-type', 'application/json')
    res.end(JSON.stringify({ peer: req.headers[PEER] ?? null, key: req.headers[KEY] ?? null }))
  }
  return {
    name: 'test-echo',
    configureServer: (server) => void server.middlewares.use(handler),
    configurePreviewServer: (server) => void server.middlewares.use(handler),
  }
}

function urlOf(server: { httpServer: { address(): AddressInfo | string | null } | null }): string {
  const address = server.httpServer?.address() as AddressInfo
  return `http://127.0.0.1:${address.port}`
}

async function forged(base: string): Promise<{ peer: string | null; key: string | null }> {
  const res = await fetch(`${base}/echo`, { headers: { [DEV_PEER_HEADER]: '10.9.8.7', [DEV_PEER_KEY_HEADER]: 'guessed' } })
  return (await res.json()) as { peer: string | null; key: string | null }
}

// The real Vite servers, with the plugin listed after the echo to show that
// `enforce: 'pre'` still puts the stamp first.
describe('in a running Vite server', () => {
  const guard = devPeerGuard()
  let root: string
  let dev: ViteDevServer
  let prev: PreviewServer

  beforeAll(async () => {
    root = mkdtempSync(join(tmpdir(), 'peer-stamp-'))
    mkdirSync(join(root, 'dist'))
    writeFileSync(join(root, 'dist', 'index.html'), '<!doctype html><title>t</title>')
    const common = { configFile: false as const, root, logLevel: 'silent' as const, plugins: [echo(), guard.plugin] }
    dev = await createServer({ ...common, server: { host: '127.0.0.1', port: 0, strictPort: false, ws: false } })
    await dev.listen()
    prev = await preview({ ...common, build: { outDir: 'dist' }, preview: { host: '127.0.0.1', port: 0 } })
  })

  afterAll(async () => {
    await dev?.close()
    await new Promise<void>((resolve) => (prev ? prev.httpServer.close(() => resolve()) : resolve()))
    rmSync(root, { recursive: true, force: true })
  })

  it('vite dev: the headers carry the socket peer and the key, not what the client sent', async () => {
    // The only peer a test can be is loopback; what matters is that the value
    // came from the socket (unit test above: a LAN peer is stamped as itself).
    expect(await forged(urlOf(dev))).toEqual({ peer: '127.0.0.1', key: guard.key })
  })

  it('vite preview: same', async () => {
    expect(await forged(urlOf(prev))).toEqual({ peer: '127.0.0.1', key: guard.key })
  })
})

describe('vite.config.ts', () => {
  it('registers the stamp ahead of the Cloudflare plugin, gives it the key, and filters builds', () => {
    const src = readFileSync(new URL('../vite.config.ts', import.meta.url), 'utf8')
    const stamp = src.indexOf('guard.plugin')
    const worker = src.indexOf('cloudflare(')
    expect(stamp).toBeGreaterThan(-1)
    expect(stamp).toBeLessThan(worker)
    expect(src).toContain('workerVarsFor(env, guard)')
    expect(src).toContain('keepBypassOutOfBuilds()')
  })
})

// `vite preview` serves the build: keep the variable out of it as well.
describe('stripBypassFromBundle', () => {
  const bundleWith = (source: string | Uint8Array) =>
    ({ '.dev.vars': { type: 'asset', fileName: '.dev.vars', source } }) as unknown as Rolldown.OutputBundle
  const sourceOf = (b: Rolldown.OutputBundle) => (b['.dev.vars'] as Rolldown.OutputAsset | undefined)?.source

  it('drops the bypass and keeps the other variables', () => {
    const b = bundleWith("DEV_AUTH_BYPASS='1'\nACCESS_AUD='abc'\n")
    stripBypassFromBundle(b)
    expect(sourceOf(b)).toBe("ACCESS_AUD='abc'\n")
  })

  it('drops a file that held nothing else, in any spelling of the line', () => {
    for (const text of ["DEV_AUTH_BYPASS='1'\n", '  DEV_AUTH_BYPASS = 1\n# local\n', 'export DEV_AUTH_BYPASS=1']) {
      const b = bundleWith(new TextEncoder().encode(text))
      stripBypassFromBundle(b)
      expect(b['.dev.vars']).toBeUndefined()
    }
  })

  it('leaves a bundle without .dev.vars alone', () => {
    const b = {} as Rolldown.OutputBundle
    stripBypassFromBundle(b)
    expect(b).toEqual({})
  })
})
