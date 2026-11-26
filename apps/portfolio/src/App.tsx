import type { ComponentType } from 'react'

import { Layout } from './components/Layout.tsx'
import { PortfolioProvider } from './data/PortfolioProvider.tsx'
import { RouterProvider } from './lib/router.tsx'
import { useRouter } from './lib/routerContext.ts'
import { ThemeProvider } from './lib/theme.tsx'
import { BuiltPage } from './pages/BuiltPage.tsx'
import { NotFoundPage } from './pages/NotFoundPage.tsx'
import { OverviewPage } from './pages/OverviewPage.tsx'
import { RecapsPage } from './pages/RecapsPage.tsx'
import { StrategyPage } from './pages/StrategyPage.tsx'
import { TradesPage } from './pages/TradesPage.tsx'

const ROUTES: Record<string, ComponentType> = {
  '/': OverviewPage,
  '/trades': TradesPage,
  '/strategy': StrategyPage,
  '/how-its-built': BuiltPage,
  '/recaps': RecapsPage,
}

function CurrentPage() {
  const { path } = useRouter()
  const Component = ROUTES[path] ?? NotFoundPage
  return <Component key={path} />
}

export default function App() {
  return (
    <ThemeProvider>
      <RouterProvider>
        <PortfolioProvider>
          <Layout>
            <CurrentPage />
          </Layout>
        </PortfolioProvider>
      </RouterProvider>
    </ThemeProvider>
  )
}
