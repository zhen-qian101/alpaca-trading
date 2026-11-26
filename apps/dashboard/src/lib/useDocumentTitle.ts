import { useEffect } from 'react'

/** "<page> · Alpaca Terminal" while the page is mounted. */
export function useDocumentTitle(page: string): void {
  useEffect(() => {
    document.title = `${page} — Alpaca Terminal`
  }, [page])
}
