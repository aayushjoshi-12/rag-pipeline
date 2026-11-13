import { create } from 'zustand'
import { api } from '../api/client'
import type { ChatResponse, Document, Job } from '../types'

interface AppState {
  documents: Document[]
  jobs: Record<string, Job>
  loading: boolean
  error: string | null
  chat: { question: string; response: ChatResponse | null; pending: boolean; error: string | null }
  refreshDocuments: () => Promise<void>
  trackJob: (job: Job) => void
  pollJob: (jobId: string) => Promise<Job>
  removeDocument: (id: string) => Promise<void>
  ask: (question: string, documentIds?: string[]) => Promise<void>
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export const useStore = create<AppState>((set, get) => ({
  documents: [],
  jobs: {},
  loading: false,
  error: null,
  chat: { question: '', response: null, pending: false, error: null },

  refreshDocuments: async () => {
    set({ loading: true, error: null })
    try {
      set({ documents: await api.listDocuments(), loading: false })
    } catch (e) {
      set({ loading: false, error: message(e) })
    }
  },

  trackJob: (job) => set((s) => ({ jobs: { ...s.jobs, [job.id]: job } })),

  pollJob: async (jobId) => {
    const job = await api.getJob(jobId)
    get().trackJob(job)
    return job
  },

  removeDocument: async (id) => {
    await api.deleteDocument(id)
    set((s) => ({ documents: s.documents.filter((d) => d.id !== id) }))
  },

  ask: async (question, documentIds) => {
    set({ chat: { question, response: null, pending: true, error: null } })
    try {
      const response = await api.chat({
        question,
        ...(documentIds && documentIds.length > 0 ? { document_ids: documentIds } : {}),
      })
      set({ chat: { question, response, pending: false, error: null } })
    } catch (e) {
      set({ chat: { question, response: null, pending: false, error: message(e) } })
    }
  },
}))
