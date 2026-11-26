import type { EngineStatus, EngineSymbol } from '../../contract.ts'

/** Three states, never two: a process that exists but stopped polling is not "live". */
export function engineState(e: EngineStatus): { label: string; tone: 'ok' | 'warn' | 'bad'; detail: string } {
  if (e.running && e.polling) return { label: 'Engine live', tone: 'ok', detail: 'Running and polling on schedule.' }
  if (e.running)
    return {
      label: 'Engine stalled',
      tone: 'warn',
      detail: `The process is up but no poll has finished in the last ${Math.round(e.stale_after_sec / 60)} min.`,
    }
  return { label: 'Engine down', tone: 'bad', detail: 'No engine process on the home box.' }
}

export function lag(behind: number | null): { text: string; lagging: boolean } {
  if (behind == null) return { text: '—', lagging: false }
  if (behind <= 60) return { text: 'current', lagging: false }
  const min = Math.round(behind / 60)
  return { text: min < 90 ? `${min} min behind` : `${Math.round(behind / 3600)} h behind`, lagging: true }
}

/**
 * A symbol's bar status for the per-symbol grid. A symbol the engine no
 * longer polls (enabled === false: disabled in sigengine/config.json) is
 * neutral, never lag: its last bar is old because nobody asks for a new one.
 * Only an explicit false means that. null (the publisher could not read the
 * config) and absent (an older publisher) show the real lag, so a broken
 * config never hides a stalled feed.
 */
export function barStatus(s: EngineSymbol): { text: string; tone: 'ok' | 'lagging' | 'off' } {
  if (s.enabled === false) return { text: 'disabled — not polled', tone: 'off' }
  const l = lag(s.behind_s)
  return { text: l.text, tone: l.lagging ? 'lagging' : 'ok' }
}

/** Polled symbols first, then the ones the engine no longer polls; otherwise as given. */
export function pollOrder(symbols: readonly EngineSymbol[]): EngineSymbol[] {
  return [...symbols.filter((s) => s.enabled !== false), ...symbols.filter((s) => s.enabled === false)]
}
