// Renders the whole app at a path over the synthetic fixture, for tests that
// check one part of a page (src/App.test.tsx keeps its own, richer harness).
import { render, screen, waitFor } from '@testing-library/react'
import { expect, vi } from 'vitest'

import fixture from '../../fixtures/portfolio.json'
import App from '../App.tsx'

export async function renderAppAt(path: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(fixture), { status: 200, headers: { 'content-type': 'application/json' } })),
  )
  window.history.replaceState(null, '', path)
  const utils = render(<App />)
  await waitFor(() => expect(screen.queryByText(/Loading the latest published figures/)).not.toBeInTheDocument())
  return utils
}
