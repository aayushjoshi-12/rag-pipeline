import { render } from '@testing-library/react'
import { vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { useStore } from '../store'
import { useAuth } from '../store/auth'
import type { api } from '../api/client'
import type { DayUsage, Document, Insights, Job } from '../types'

export const doc = (over: Partial<Document> = {}): Document => ({
  id: 'd1000000-0000-0000-0000-000000000001',
  filename: 'report.pdf',
  content_type: 'application/pdf',
  status: 'ready',
  created_at: '2026-11-13T10:00:00',
  updated_at: '2026-11-13T10:00:00',
  ...over,
})

export const job = (over: Partial<Job> = {}): Job => ({
  id: 'j1000000-0000-0000-0000-000000000001',
  document_id: doc().id,
  task_id: 't1',
  status: 'pending',
  error: null,
  stats: null,
  created_at: '2026-11-13T10:00:00',
  updated_at: '2026-11-13T10:00:00',
  ...over,
})

export const ALL_PERMISSIONS = [
  'documents:read',
  'documents:write',
  'documents:delete',
  'jobs:read',
  'chat:use',
  'traces:read',
  'users:manage',
  'roles:manage',
  'apikeys:manage',
  'settings:read',
  'settings:manage',
]

/** Signed in as ``email`` holding ``permissions`` (default: everything). */
export function signInAs(permissions: string[] = ALL_PERMISSIONS, email = 'admin@example.test') {
  useAuth.setState({
    status: 'authenticated',
    error: null,
    me: { id: 'u1', email, permissions, auth_enabled: true },
  })
}

export function resetStore() {
  signInAs()
  useStore.setState({
    documents: [],
    jobs: {},
    loading: false,
    error: null,
    chat: { question: '', response: null, pending: false, error: null },
  })
}

export const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )

/** Fresh state for a page test: clear mocks, sign in as admin, empty document list, healthy API. */
export function resetApp(m: {
  listDocuments: { mockResolvedValue: (v: never[]) => unknown }
  health: (typeof api)['health'] & { mockResolvedValue: (v: never) => unknown }
}) {
  vi.clearAllMocks()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} } as never)
}

export const chatSource = (over: Record<string, unknown> = {}) => ({
  text: 'passage',
  document_id: 'd1000000-0000-0000-0000-000000000001',
  chunk_index: 1,
  filename: 'a.md',
  score: 0.5,
  ...over,
})

export const dayUsage = (over: Partial<DayUsage> = {}): DayUsage => ({
  day: '2026-11-14',
  requests: 10,
  errors: 2,
  avg_ms: 500,
  p95_ms: 900,
  prompt_tokens: 1000,
  completion_tokens: 200,
  cost_usd: null,
  unpriced_requests: 0,
  ...over,
})

export const baseInsights = (over: Partial<Insights> = {}): Insights => ({
  days: 14,
  kind: null,
  cost_usd: null,
  priced_chats: 0,
  unpriced_chats: 0,
  avg_cost_per_chat: null,
  by_model: [],
  latency_bucket_ms: 100,
  latency_histogram: [],
  errors_by_reason: [],
  heatmap: [],
  rag_days: [],
  hits_histogram: [],
  top_documents: [],
  cited_documents: 0,
  total_documents: 0,
  never_cited: 0,
  reuse_days: [],
  ingest_modes: [],
  ingest_failures: [],
  ...over,
})
