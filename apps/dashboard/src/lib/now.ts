import { createContext, useContext } from 'react'

/** Wall-clock milliseconds, re-published once a second by NowProvider. */
export const NowContext = createContext<number>(Date.now())

/** The current time, ticking every second, for ages and countdowns. */
export function useNow(): number {
  return useContext(NowContext)
}
