import { createContext, useContext } from 'react'

export interface RouterValue {
  path: string
  navigate: (to: string) => void
  /** True after the first client-side navigation (focus moves on route change only). */
  navigated: boolean
}

export const RouterContext = createContext<RouterValue>({ path: '/', navigate: () => {}, navigated: false })

export function useRouter(): RouterValue {
  return useContext(RouterContext)
}

/** "/trades/" -> "/trades", "/x?y#z" -> "/x". */
export function normalizePath(path: string): string {
  const bare = path.split(/[?#]/)[0] || '/'
  return bare.length > 1 ? bare.replace(/\/+$/, '') || '/' : bare
}
