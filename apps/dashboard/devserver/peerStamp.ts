/**
 * Vite plugins that keep the Worker's dev auth bypass on this machine.
 *
 * devPeerGuard() makes a random key for this dev server process. Its plugin
 * runs a middleware ahead of the Cloudflare plugin (`enforce: 'pre'`, listed
 * first in vite.config.ts) that, on every request, REPLACES two headers:
 *   - DEV_PEER_HEADER with `req.socket.remoteAddress`, the TCP peer;
 *   - DEV_PEER_KEY_HEADER with the key.
 * Values a client sent under those names never reach the Worker. The key also
 * goes into the Worker's env as DEV_PEER_KEY, for `vite dev` only
 * (workerVarsFor). worker/auth.ts grants DEV_AUTH_BYPASS only when the key
 * matches and the peer is loopback, so:
 *   - a dev server started with `--host` still refuses every phone or laptop
 *     on the network, whatever Host header it sends;
 *   - a runtime that does not run this middleware cannot be talked into the
 *     bypass with forged headers. `wrangler dev` reads the same .dev.vars but
 *     has no key; `vite preview` has neither the key nor (see
 *     keepBypassOutOfBuilds) the variable.
 *
 * See worker/devPeer.ts for the rest of the rule.
 */
import { randomBytes } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { ConfigEnv, Plugin, Rolldown } from 'vite'

import { DEV_PEER_HEADER, DEV_PEER_KEY_HEADER, DEV_PEER_KEY_VAR } from '../worker/devPeer.ts'

/** Node lower-cases incoming header names. */
const PEER = DEV_PEER_HEADER.toLowerCase()
const KEY = DEV_PEER_KEY_HEADER.toLowerCase()

type Middleware = (req: IncomingMessage, res: ServerResponse, next: () => void) => void

/** Connect middleware: overwrite both headers with the connection's peer and this process's key. */
export function stampPeer(key: string): Middleware {
  return (req, _res, next) => {
    // Always assigned, never appended: a client-sent value (or several) is gone.
    req.headers[PEER] = req.socket.remoteAddress ?? ''
    req.headers[KEY] = key
    next()
  }
}

export interface DevPeerGuard {
  plugin: Plugin
  /** The key, for workerVarsFor; it never leaves this process except into workerd. */
  key: string
}

export function devPeerGuard(): DevPeerGuard {
  const key = randomBytes(32).toString('hex')
  const stamp = stampPeer(key)
  return {
    key,
    plugin: {
      name: 'alpaca-dev-peer-stamp',
      enforce: 'pre',
      // Registered directly in the hook (not in a returned post hook), so the
      // middleware sits ahead of every middleware Vite and later plugins add.
      configureServer(server) {
        server.middlewares.use(stamp)
      },
      configurePreviewServer(server) {
        server.middlewares.use(stamp)
      },
    },
  }
}

/**
 * Worker vars for the Cloudflare plugin's `config`: the key under `vite dev`
 * only. Never in a build (the vars would land in the deployable
 * wrangler.json) and never in preview (which serves that build).
 */
export function workerVarsFor(env: ConfigEnv, guard: DevPeerGuard): Record<string, string> | undefined {
  return env.command === 'serve' && !env.isPreview ? { [DEV_PEER_KEY_VAR]: guard.key } : undefined
}

const BYPASS_LINE = /^\s*(?:export\s+)?DEV_AUTH_BYPASS\s*=/

/**
 * Remove DEV_AUTH_BYPASS from a `.dev.vars` asset in `bundle` (the copy the
 * Cloudflare plugin writes next to the built Worker for `vite preview`).
 * Other variables stay; a file left with none is dropped.
 */
export function stripBypassFromBundle(bundle: Rolldown.OutputBundle): void {
  const asset = bundle['.dev.vars']
  if (!asset || asset.type !== 'asset') return
  const text = typeof asset.source === 'string' ? asset.source : new TextDecoder().decode(asset.source)
  const kept = text.split('\n').filter((line) => !BYPASS_LINE.test(line))
  if (kept.every((line) => line.trim() === '' || line.trimStart().startsWith('#'))) {
    delete bundle['.dev.vars']
  } else {
    asset.source = kept.join('\n')
  }
}

/** The build output never carries DEV_AUTH_BYPASS, whatever .dev.vars holds. */
export function keepBypassOutOfBuilds(): Plugin {
  return {
    name: 'alpaca-keep-dev-bypass-out-of-builds',
    apply: 'build',
    // After the Cloudflare plugin has emitted the asset in its own generateBundle.
    enforce: 'post',
    generateBundle(_options, bundle) {
      stripBypassFromBundle(bundle)
    },
  }
}
