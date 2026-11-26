import { useEffect, useState, type ReactNode } from 'react'

import { loadPortfolio, type LoadState } from './load.ts'
import { PortfolioContext } from './portfolioContext.ts'

/** Loads /data/portfolio.json once per visit and shares the result. */
export function PortfolioProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  useEffect(() => {
    let live = true
    void loadPortfolio().then((next) => {
      if (live) setState(next)
    })
    return () => {
      live = false
    }
  }, [])
  return <PortfolioContext.Provider value={state}>{children}</PortfolioContext.Provider>
}
