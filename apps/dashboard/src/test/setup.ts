// Adds the jest-dom matchers (toBeInTheDocument, toHaveTextContent, ...) to
// vitest's expect, and unmounts rendered trees between tests.
import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, vi } from 'vitest'

// jsdom has no media pipeline: make the <video> methods the Briefing page
// calls into harmless no-ops instead of "not implemented" console errors.
Object.defineProperty(window.HTMLMediaElement.prototype, 'load', { configurable: true, value: () => {} })
Object.defineProperty(window.HTMLMediaElement.prototype, 'pause', { configurable: true, value: () => {} })
Object.defineProperty(window.HTMLMediaElement.prototype, 'play', {
  configurable: true,
  value: () => Promise.resolve(),
})

// React reports render bugs (a missing list key, an update on an unmounted
// component, invalid DOM nesting) through console.error and carries on, so a
// test would pass while the page logs errors. Fail the test instead.
let consoleErrors: unknown[][] = []
let consoleSpy: { mockRestore: () => void } | null = null
beforeEach(() => {
  consoleErrors = []
  consoleSpy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    consoleErrors.push(args)
  })
})

afterEach(() => {
  cleanup()
  const logged = consoleErrors.map((args) => args.map(String).join(' '))
  consoleSpy?.mockRestore()
  consoleSpy = null
  expect(logged, 'console.error during the test').toEqual([])
  vi.useRealTimers()
  vi.unstubAllGlobals()
  try {
    window.localStorage.clear()
  } catch {
    // storage unavailable
  }
  delete document.documentElement.dataset.theme
})
