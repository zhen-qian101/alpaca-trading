import { createContext, useContext } from 'react'

import type { LoadState } from './load.ts'

export const PortfolioContext = createContext<LoadState>({ status: 'loading' })

export function usePortfolio(): LoadState {
  return useContext(PortfolioContext)
}
