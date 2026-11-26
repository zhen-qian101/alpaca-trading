import { useEffect, useRef, useState, type ReactNode } from 'react'

/**
 * Whether the region still has columns hidden past its right edge. Drives the
 * fade that tells a phone reader the table scrolls sideways; it clears once
 * the last column is in view. Without ResizeObserver it rechecks on window
 * resizes only.
 */
function useMoreToTheRight() {
  const ref = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setMore(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
    update()
    el.addEventListener('scroll', update, { passive: true })
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update)
      return () => {
        el.removeEventListener('scroll', update)
        window.removeEventListener('resize', update)
      }
    }
    // The region (the viewport changed) and the table (its rows were re-sorted).
    const observer = new ResizeObserver(update)
    observer.observe(el)
    if (el.firstElementChild) observer.observe(el.firstElementChild)
    return () => {
      el.removeEventListener('scroll', update)
      observer.disconnect()
    }
  }, [])
  return [ref, more] as const
}

/**
 * A ruled ledger table in a keyboard-scrollable region. The note is shown
 * above the region (a caption inside a scrolling table would scroll away with
 * it) and is also the table's caption for screen readers, so it is read once.
 * While columns are hidden past the right edge, that edge fades out.
 */
export function LedgerTable({
  label,
  note,
  children,
  className = '',
}: {
  /** Names the scroll region. */
  label: string
  note: ReactNode
  children: ReactNode
  className?: string
}) {
  const [scrollRef, more] = useMoreToTheRight()
  return (
    <>
      <p className="table-note" aria-hidden="true">
        {note}
      </p>
      <div className="table-frame" data-more={more ? 'true' : undefined}>
        <div ref={scrollRef} className="table-scroll" tabIndex={0} role="region" aria-label={label}>
          <table className={`ledger ${className}`}>
            <caption className="visually-hidden">{note}</caption>
            {children}
          </table>
        </div>
      </div>
    </>
  )
}
