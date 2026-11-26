/**
 * A minimal client router: five fixed pages do not need a routing library.
 * Paths are plain History API entries; unknown paths render the not-found
 * page (Workers Static Assets answers every unknown path with index.html).
 */
import { useCallback, useEffect, useMemo, useState, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from 'react'

import { normalizePath, RouterContext, useRouter } from './routerContext.ts'

export function RouterProvider({ children }: { children: ReactNode }) {
  const [path, setPath] = useState(() => normalizePath(window.location.pathname))
  const [navigated, setNavigated] = useState(false)

  useEffect(() => {
    const onPop = () => {
      setPath(normalizePath(window.location.pathname))
      setNavigated(true)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback((to: string) => {
    const next = normalizePath(to)
    if (next !== normalizePath(window.location.pathname)) window.history.pushState(null, '', next)
    setPath(next)
    setNavigated(true)
    window.scrollTo(0, 0)
  }, [])

  const value = useMemo(() => ({ path, navigate, navigated }), [path, navigate, navigated])
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & { to: string; children: ReactNode }

/** An in-site link: a real <a href>, handled without a page load. */
export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const { navigate } = useRouter()
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event)
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      rest.target
    )
      return
    event.preventDefault()
    navigate(to)
  }
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  )
}
