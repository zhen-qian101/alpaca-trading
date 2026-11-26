/**
 * Light and dark themes. The page follows the system setting until the
 * visitor picks one with the toggle; that choice is remembered in this
 * browser only (localStorage, best effort). public/theme-init.js applies a
 * remembered choice before first paint, without an inline script.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { THEME_KEY, ThemeContext, type Theme } from './themeContext.ts'

function storedChoice(): Theme | null {
  try {
    const v = window.localStorage.getItem(THEME_KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

function systemTheme(): Theme {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoice] = useState<Theme | null>(storedChoice)
  const [system, setSystem] = useState<Theme>(systemTheme)

  useEffect(() => {
    let mq: MediaQueryList
    try {
      mq = window.matchMedia('(prefers-color-scheme: dark)')
    } catch {
      return
    }
    const onChange = () => setSystem(mq.matches ? 'dark' : 'light')
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])

  const theme = choice ?? system

  useEffect(() => {
    const root = document.documentElement
    if (choice) root.dataset.theme = choice
    else delete root.dataset.theme
  }, [choice])

  const toggle = useCallback(() => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    setChoice(next)
    try {
      window.localStorage.setItem(THEME_KEY, next)
    } catch {
      // storage unavailable: the choice lasts for this visit
    }
  }, [theme])

  const value = useMemo(() => ({ theme, toggle }), [theme, toggle])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
