import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Kept separate from vite.config.ts: tests need neither Tailwind nor the
// dev-data and head plugins.
//
// Two projects:
//   app     - the SPA, in jsdom, with @testing-library/jest-dom matchers.
//   config  - deployment and security invariants read from the files
//             themselves (wrangler.jsonc, _headers, index.html), in Node.
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
          name: 'config',
          environment: 'node',
          include: ['scripts/**/*.test.ts'],
        },
      },
    ],
  },
})
