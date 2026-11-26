import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Kept separate from vite.config.ts on purpose: the Cloudflare plugin there
// boots workerd, which unit tests neither need nor want.
//
// Two projects:
//   app    - the SPA, in jsdom, with @testing-library/jest-dom matchers.
//   worker - the Worker's request handling, in plain Node. Node 22 provides
//            the same web-standard Request/Response/fetch/crypto.subtle the
//            Workers runtime does, so the handler is exercised directly with
//            an in-memory stand-in for the R2 binding. (@cloudflare/vitest-
//            pool-workers is not used: its current release requires vitest 4
//            and pins an older wrangler.) It also runs devserver/, the Vite
//            plugin that stamps the dev auth bypass's peer address.
export default defineConfig({
  test: {
    projects: [
      {
        plugins: [react()],
        test: {
          name: 'app',
          environment: 'jsdom',
          include: ['src/**/*.test.{ts,tsx}'],
          setupFiles: ['./src/test/setup.ts'],
        },
      },
      {
        test: {
          name: 'worker',
          environment: 'node',
          include: ['worker/**/*.test.ts', 'devserver/**/*.test.ts'],
        },
      },
    ],
  },
})
