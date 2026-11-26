import { Page } from '../components/Layout.tsx'
import { Link } from '../lib/router.tsx'

export function NotFoundPage() {
  return (
    <Page title="Page not found" heading="Page not found">
      <p className="prose">
        There is no page at this address. Start from the <Link to="/">overview</Link>.
      </p>
    </Page>
  )
}
