import { createContext, useContext } from 'react'

export type Theme = 'light' | 'dark'

/** localStorage key; public/theme-init.js reads the same one. */
export const THEME_KEY = 'portfolio-theme'

export interface ThemeValue {
  theme: Theme
  toggle: () => void
}

export const ThemeContext = createContext<ThemeValue>({ theme: 'light', toggle: () => {} })

export function useTheme(): ThemeValue {
  return useContext(ThemeContext)
}
