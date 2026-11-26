/**
 * localStorage for per-viewer conveniences only (theme, time zone). Storage
 * can be missing or throw (private windows, blocked site data), so every
 * access is guarded and the app works the same without it.
 */
const PREFIX = 'alpaca-dashboard:'

export function readPref(key: string): string | null {
  try {
    return window.localStorage.getItem(PREFIX + key)
  } catch {
    return null
  }
}

export function writePref(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(PREFIX + key)
    else window.localStorage.setItem(PREFIX + key, value)
  } catch {
    // storage unavailable: the choice lasts for this page view only
  }
}
