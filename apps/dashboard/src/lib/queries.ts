import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  FRESHNESS,
  type ApproveBody,
  type ApproveResponse,
  type ChartResponse,
  type History,
  type SnapshotResponse,
} from '../contract.ts'
import { apiFetch } from './api.ts'

export const queryKeys = {
  snapshot: ['snapshot'] as const,
  chart: (symbol: string) => ['chart', symbol] as const,
  history: ['history'] as const,
}

/** GET /api/snapshot every 30 s while the tab is visible. */
export function useSnapshot() {
  return useQuery({
    queryKey: queryKeys.snapshot,
    queryFn: ({ signal }) => apiFetch<SnapshotResponse>('/api/snapshot', { signal }),
    refetchInterval: FRESHNESS.snapshotPollMs,
    staleTime: FRESHNESS.snapshotPollMs / 2,
  })
}

/** GET /api/chart?symbol=SYM every 60 s while the tab is visible. */
export function useChart(symbol: string | null) {
  return useQuery({
    queryKey: queryKeys.chart(symbol ?? ''),
    queryFn: ({ signal }) =>
      apiFetch<ChartResponse>(`/api/chart?symbol=${encodeURIComponent(symbol ?? '')}`, { signal }),
    enabled: symbol !== null,
    refetchInterval: FRESHNESS.chartPollMs,
    staleTime: FRESHNESS.chartPollMs / 2,
  })
}

/** GET /media/history.json. Editions change a few times a day. */
export function useHistory() {
  return useQuery({
    queryKey: queryKeys.history,
    queryFn: ({ signal }) => apiFetch<History>('/media/history.json', { signal }),
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
  })
}

/**
 * POST /api/look/approve. On success the recorded request is written into the
 * cached snapshot straight away, so the page shows "requested, waiting for the
 * home box" without waiting for the next poll; the snapshot is then refetched.
 */
export function useApproveLook() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ApproveBody) =>
      apiFetch<ApproveResponse>('/api/look/approve', { method: 'POST', json: body }),
    onSuccess: (response) => {
      client.setQueryData<SnapshotResponse>(queryKeys.snapshot, (old) =>
        old ? { ...old, approval_request: response.request } : old,
      )
      void client.invalidateQueries({ queryKey: queryKeys.snapshot })
    },
  })
}
