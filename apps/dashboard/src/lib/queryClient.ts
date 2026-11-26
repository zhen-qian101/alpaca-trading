import { QueryClient } from '@tanstack/react-query'

import { isAuthError, isNotFound } from './api.ts'

/** Attempts after the first failure before a query reports an error. */
export const MAX_RETRIES = 3

/** Exponential backoff with jitter: ~1 s, 2 s, 4 s ... capped at 20 s. */
export function retryDelay(attempt: number): number {
  const base = Math.min(1000 * 2 ** attempt, 20_000)
  return base + Math.round(Math.random() * 250)
}

/** Signed-out and not-published-yet are answers, not glitches: never retry them. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isAuthError(error) || isNotFound(error)) return false
  return failureCount < MAX_RETRIES
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay,
        // Polling pauses while the tab is hidden (TanStack's default for
        // refetchInterval) and catches up as soon as it is visible again.
        refetchIntervalInBackground: false,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
      },
      mutations: { retry: false },
    },
  })
}
