import { create } from 'zustand'
import { api } from '../api/client'
import { StreamUnavailableError } from '../api/stream'
import type { ChatDone, ChatRequest, ChatResponse, Document, Job } from '../types'

export interface ChatState {
  question: string
  /** Assembled incrementally while streaming: sources arrive first, `answer` grows token by token. */
  response: ChatResponse | null
  /** A request is in flight (stream open or waiting for sources). */
  pending: boolean
  error: string | null
  /** Tokens may still arrive (the stream is open and `response` exists). */
  streaming?: boolean
  /** The user pressed Stop; `response` holds the partial answer, if any. */
  stopped?: boolean
  /** From the final `done` event. */
  done?: ChatDone
}

interface AppState {
  documents: Document[]
  jobs: Record<string, Job>
  loading: boolean
  error: string | null
  chat: ChatState
  refreshDocuments: () => Promise<void>
  trackJob: (job: Job) => void
  pollJob: (jobId: string) => Promise<Job>
  removeDocument: (id: string) => Promise<void>
  ask: (
    question: string,
    documentIds?: string[],
    retrieval?: Pick<ChatRequest, 'mode' | 'fusion' | 'top_k' | 'neighbors'>,
  ) => Promise<void>
  /** Abort the running chat request (the server then cancels the upstream LLM call). */
  stopChat: () => void
}

let chatAbort: AbortController | null = null

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

  stopChat: () => chatAbort?.abort(),

  ask: async (question, documentIds, retrieval) => {
    chatAbort?.abort()
    const controller = new AbortController()
    chatAbort = controller
    const req: ChatRequest = {
      question,
      ...(documentIds && documentIds.length > 0 ? { document_ids: documentIds } : {}),
      ...retrieval,
    }
    // This request's state only; a newer request replaces `chat` entirely, so stale events are dropped.
    const update = (patch: Partial<ChatState>) => {
      if (chatAbort === controller) set((s) => ({ chat: { ...s.chat, ...patch } }))
    }
    set({ chat: { question, response: null, pending: true, error: null } })

    let terminal = false
    let tokens = 0
    try {
      await api.chatStream(
        req,
        (e) => {
          if (e.event === 'sources') {
            update({ response: { answer: '', sources: e.data.sources }, streaming: true })
          } else if (e.event === 'token') {
            tokens++
            set((s) =>
              chatAbort === controller && s.chat.response
                ? {
                    chat: {
                      ...s.chat,
                      response: {
                        ...s.chat.response,
                        answer: s.chat.response.answer + e.data.text,
                      },
                    },
                  }
                : s,
            )
          } else if (e.event === 'done') {
            terminal = true
            set((s) =>
              chatAbort === controller && s.chat.response
                ? {
                    chat: {
                      ...s.chat,
                      response: { ...s.chat.response, answer: e.data.answer },
                      pending: false,
                      streaming: false,
                      done: e.data,
                    },
                  }
                : s,
            )
          } else {
            terminal = true
            update({ pending: false, streaming: false, error: e.data.detail })
          }
        },
        controller.signal,
      )
      if (!terminal) {
        if (tokens === 0) throw new StreamUnavailableError('the stream ended without an answer')
        update({ pending: false, streaming: false, error: 'The answer stream ended unexpectedly.' })
      }
    } catch (e) {
      if (controller.signal.aborted) {
        update({ pending: false, streaming: false, stopped: true })
      } else if (e instanceof StreamUnavailableError) {
        // could not open the stream (old backend, proxy that breaks SSE): the plain endpoint still works
        try {
          const response = await api.chat(req)
          update({ response, pending: false, streaming: false, error: null })
        } catch (e2) {
          if (controller.signal.aborted) update({ pending: false, stopped: true })
          else update({ response: null, pending: false, streaming: false, error: message(e2) })
        }
      } else {
        update({ pending: false, streaming: false, error: message(e) })
      }
    } finally {
      if (chatAbort === controller) chatAbort = null
    }
  },
}))
