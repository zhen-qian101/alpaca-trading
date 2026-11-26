import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'

import { AppRoutes } from '../app/AppRoutes.tsx'
import { Providers } from '../app/Providers.tsx'
import { createQueryClient } from '../lib/queryClient.ts'

/** Freeze Date (only) at an instant; timers keep running so Testing Library can wait. */
export function freezeTime(iso: string): void {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(iso))
}

/** Render the whole app (shell + routes) at `path` with a fresh query cache and no retries. */
export function renderApp(path = '/') {
  const client = createQueryClient()
  client.setDefaultOptions({
    queries: { ...client.getDefaultOptions().queries, retry: false },
    mutations: { retry: false },
  })
  const utils = render(
    <Providers client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </Providers>,
  )
  return { ...utils, client }
}
