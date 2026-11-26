/** The site's mark: a rising equity step over a bookkeeper's double rule. */
export function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="6" fill="currentColor" />
      <path
        d="M6.5 20.5h5v-5h5v-4h5v-5h4"
        fill="none"
        stroke="var(--paper)"
        strokeWidth="2.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <path d="M6 24.6h20M6 27.4h20" stroke="var(--paper)" strokeWidth="1.3" />
    </svg>
  )
}
