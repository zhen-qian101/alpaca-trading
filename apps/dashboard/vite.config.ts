import { cloudflare } from '@cloudflare/vite-plugin'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

import { devPeerGuard, keepBypassOutOfBuilds, workerVarsFor } from './devserver/peerStamp.ts'

// One build produces both halves: the SPA (client environment, served as
// Static Assets) and the Worker in worker/index.ts (run by workerd in `vite
// dev`, with a local R2 simulation under .wrangler/state).
// https://developers.cloudflare.com/workers/vite-plugin/
//
// ALPACA_DASHBOARD_STATE_DIR points the local simulation somewhere else, e.g.
// a scratch folder seeded with `pnpm seed:local --from-dir ... --persist-to`
// from a real publisher run, so real data never sits inside the repository.
//
// The dev auth bypass (worker/devPeer.ts): the guard's plugin comes first and
// stamps each request with its TCP peer and this process's key before the
// Worker sees it; the key reaches the Worker as a var under `vite dev` only.
// That limits the bypass to this machine, through `vite dev`, even when the
// server is started with --host. keepBypassOutOfBuilds() also drops
// DEV_AUTH_BYPASS from the .dev.vars copy in the build.
const stateDir = process.env.ALPACA_DASHBOARD_STATE_DIR

// The SPA's build writes the licences of every package bundled into it
// (React, lightweight-charts, React Router, TanStack Query...) to
// dist/client/third-party-licenses.txt, which Static Assets serves at
// /third-party-licenses.txt. Not Vite's default .vite/license.md, under a dot
// directory. The client only: the Worker's bundle is never sent to a browser.
const LICENSES_FILE = 'third-party-licenses.txt'

export default defineConfig((env) => {
  const guard = devPeerGuard()
  const vars = workerVarsFor(env, guard)
  return {
    plugins: [
      guard.plugin,
      react(),
      tailwindcss(),
      cloudflare({ persistState: stateDir ? { path: stateDir } : true, ...(vars ? { config: { vars } } : {}) }),
      keepBypassOutOfBuilds(),
    ],
    environments: {
      client: { build: { license: { fileName: LICENSES_FILE } } },
    },
  }
})
