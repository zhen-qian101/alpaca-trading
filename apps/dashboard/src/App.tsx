import { useState } from 'react'
import { BrowserRouter } from 'react-router'

import { AppRoutes } from './app/AppRoutes.tsx'
import { Providers } from './app/Providers.tsx'
import { createQueryClient } from './lib/queryClient.ts'

export default function App() {
  const [client] = useState(createQueryClient)
  return (
    <Providers client={client}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </Providers>
  )
}
