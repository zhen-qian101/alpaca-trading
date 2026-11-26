import { useEffect, useState } from 'react'

export interface PageVisibility {
  hidden: boolean
  /** Wall-clock ms the tab last became visible again; null until it has been hidden once. */
  resumedAt: number | null
}

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/**
 * The tab's visibility, and when it last came back. Updated in the same
 * visibilitychange event as the shared clock (NowProvider), so the first
 * render after a return already knows the page is re-checking.
 */
export function usePageVisibility(): PageVisibility {
  const [state, setState] = useState<PageVisibility>(() => ({ hidden: isHidden(), resumedAt: null }))
  useEffect(() => {
    const onChange = () => {
      const hidden = isHidden()
      setState((prev) =>
        hidden === prev.hidden ? prev : { hidden, resumedAt: hidden ? prev.resumedAt : Date.now() },
      )
    }
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])
  return state
}
