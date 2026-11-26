import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderAppAt } from '../test/renderApp.tsx'

const PATHS = ['/', '/trades', '/strategy', '/how-its-built', '/recaps', '/no-such-page']

/** Lightweight Charts' NOTICE (v5.3.0), which its Apache-2.0 licence asks a site to show its users. */
const NOTICE = 'TradingView Lightweight Charts™ Copyright (c) 2025 TradingView, Inc. https://www.tradingview.com/'

describe('footer', () => {
  it.each(PATHS)('%s shows the TradingView notice with its link', async (path) => {
    await renderAppAt(path)
    const footer = screen.getByRole('contentinfo')
    const notice = within(footer).getByText((_, el) => el?.tagName === 'P' && el.textContent === NOTICE)
    expect(within(notice).getByRole('link', { name: 'https://www.tradingview.com/' })).toHaveAttribute(
      'href',
      'https://www.tradingview.com/',
    )
  })

  it.each(PATHS)('%s links the open-source licences file the build writes', async (path) => {
    await renderAppAt(path)
    const link = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'Open-source licences' })
    expect(link).toHaveAttribute('href', '/third-party-licenses.txt')
  })

  it('leaves the licences link to the browser instead of routing it to the not-found page', async () => {
    await renderAppAt('/')
    const link = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'Open-source licences' })
    // Runs after the app's own click handling; jsdom cannot load the file, so stop it here.
    let routed: boolean | null = null
    const stop = (event: Event) => {
      routed = event.defaultPrevented
      event.preventDefault()
    }
    window.addEventListener('click', stop)
    try {
      link.click()
    } finally {
      window.removeEventListener('click', stop)
    }
    expect(routed).toBe(false)
    expect(screen.queryByRole('heading', { level: 1, name: /not found/i })).not.toBeInTheDocument()
  })
})
