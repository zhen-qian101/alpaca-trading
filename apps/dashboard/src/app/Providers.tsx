import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { NowContext } from '../lib/now.ts'
import { PrefsContext, type Prefs, type Theme } from '../lib/prefs.ts'
import { readPref, writePref } from '../lib/storage.ts'
import { DEFAULT_ZONE, isZoneKey, type ZoneKey } from '../lib/time.ts'

const LIGHT_QUERY = '(prefers-color-scheme: light)'

function systemTheme(): Theme {
  try {
    return typeof window.matchMedia === 'function' && window.matchMedia(LIGHT_QUERY).matches
      ? 'light'
      : 'dark'
  } catch {
    return 'dark'
  }
}

function storedTheme(): Theme | null {
  const v = readPref('theme')
  return v === 'dark' || v === 'light' ? v : null
}

function storedZone(): ZoneKey {
  const v = readPref('zone')
  return isZoneKey(v) ? v : DEFAULT_ZONE
}

/** Theme + display zone, persisted per viewer. Dark unless the system asks for light. */
function PrefsProvider({ children }: { children: ReactNode }) {
  const [themeChoice, setThemeChoice] = useState<Theme | null>(storedTheme)
  const [system, setSystem] = useState<Theme>(systemTheme)
  const [zone, setZoneState] = useState<ZoneKey>(storedZone)

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(LIGHT_QUERY)
    const onChange = () => setSystem(mq.matches ? 'light' : 'dark')
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])

  const theme = themeChoice ?? system

  useEffect(() => {
    const root = document.documentElement
    // With no explicit choice the attribute is removed and the stylesheet's
    // prefers-color-scheme rule decides, so the first paint needs no script.
    if (themeChoice) root.dataset.theme = themeChoice
    else delete root.dataset.theme
    const meta = document.querySelector('meta[name="theme-color"]:not([media])')
    meta?.setAttribute('content', theme === 'light' ? '#f2f5f7' : '#0e1a22')
  }, [themeChoice, theme])

  const setTheme = useCallback((next: Theme) => {
    setThemeChoice(next)
    writePref('theme', next)
  }, [])

  const setZone = useCallback((next: ZoneKey) => {
    setZoneState(next)
    writePref('zone', next)
  }, [])

  const value = useMemo<Prefs>(
    () => ({ theme, themeChoice, setTheme, zone, setZone }),
    [theme, themeChoice, setTheme, zone, setZone],
  )
  return <PrefsContext.Provider value={value}>{children}</PrefsContext.Provider>
}

/** One shared one-second clock for every age and countdown on the page. */
function NowProvider({ children }: { children: ReactNode }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    const onVisible = () => {
      if (!document.hidden) setNow(Date.now())
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>
}

export function Providers({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <PrefsProvider>
        <NowProvider>{children}</NowProvider>
      </PrefsProvider>
    </QueryClientProvider>
  )
}
