// jest-dom matchers, cleanup between tests, and the browser pieces jsdom lacks.
import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, vi } from 'vitest'

import { resetChartCalls } from './chartMock.ts'

// jsdom has no canvas, so the chart library is replaced by a recorder
// (src/test/chartMock.ts).
vi.mock('lightweight-charts', async () => (await import('./chartMock.ts')).chartModule)

if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

window.scrollTo = () => {}

// React reports render bugs through console.error and carries on; fail the
// test instead.
let consoleErrors: unknown[][] = []
let consoleSpy: { mockRestore: () => void } | null = null
beforeEach(() => {
  resetChartCalls()
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
  window.history.replaceState(null, '', '/')
})
