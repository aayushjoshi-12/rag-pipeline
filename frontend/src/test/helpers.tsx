import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { useStore } from '../store'
import { useAuth } from '../store/auth'
import type { Document, Job } from '../types'

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
