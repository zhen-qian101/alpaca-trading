import { createContext, useContext } from 'react'

import { DEFAULT_ZONE, type ZoneKey } from './time.ts'

export type Theme = 'dark' | 'light'

export interface Prefs {
  /** The theme in effect. */
  theme: Theme
  /** The explicit choice, or null when following the system setting. */
  themeChoice: Theme | null
  setTheme: (theme: Theme) => void
  /** Zone every timestamp is shown in. */
  zone: ZoneKey
  setZone: (zone: ZoneKey) => void
}

export const PrefsContext = createContext<Prefs>({
  theme: 'dark',
  themeChoice: null,
  setTheme: () => {},
  zone: DEFAULT_ZONE,
  setZone: () => {},
})

export function usePrefs(): Prefs {
  return useContext(PrefsContext)
}
