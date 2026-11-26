import { Link, Route, Routes } from 'react-router'

import { BriefingPage } from '../features/briefing/BriefingPage.tsx'
import { LookPage } from '../features/look/LookPage.tsx'
import { TerminalPage } from '../features/terminal/TerminalPage.tsx'
import { useDocumentTitle } from '../lib/useDocumentTitle.ts'
import { Shell } from './Shell.tsx'

function NotFound() {
  useDocumentTitle('Not found')
  return (
    <div className="py-16">
      <h1 className="m-0 text-xl">Nothing at this address</h1>
      <p className="text-muted">
        The dashboard has three pages. <Link to="/">Open the terminal</Link>.
      </p>
    </div>
  )
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<TerminalPage />} />
        <Route path="briefing" element={<BriefingPage />} />
        <Route path="look" element={<LookPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
