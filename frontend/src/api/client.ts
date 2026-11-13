import type {
  ChatRequest,
  ChatResponse,
  Chunk,
  Document,
  DocumentDetail,
  HealthResponse,
  Job,
  UploadResponse,
} from '../types'

export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api'

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

let authToken: string | null = null
/** Bearer-ready: set a token and every request carries it. */
export function setAuthToken(token: string | null): void {
  authToken = token
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
  } catch {
    /* non-JSON error body */
  }
  return res.statusText || `HTTP ${res.status}`
}

/** The single request path: every API call goes through here, so errors are uniform. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (authToken) headers.set('Authorization', `Bearer ${authToken}`)
  if (typeof init.body === 'string') headers.set('Content-Type', 'application/json')
  let res: Response
  try {
    res = await fetch(`${BASE_URL}${path}`, { ...init, headers })
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : 'Network error')
  }
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  listDocuments: () => request<Document[]>('/documents'),
  getDocument: (id: string) => request<DocumentDetail>(`/documents/${id}`),
  listChunks: (id: string) => request<Chunk[]>(`/documents/${id}/chunks`),
  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: 'DELETE' }),
  getJob: (id: string) => request<Job>(`/jobs/${id}`),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<UploadResponse>('/documents', { method: 'POST', body: form })
  },
  replace: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<UploadResponse>(`/documents/${id}`, { method: 'PUT', body: form })
  },
  chat: (req: ChatRequest) =>
    request<ChatResponse>('/chat', { method: 'POST', body: JSON.stringify(req) }),
}
