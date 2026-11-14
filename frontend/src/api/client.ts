import type {
  ChatRequest,
  ChatResponse,
  Chunk,
  Document,
  DocumentDetail,
  HealthResponse,
  Job,
  ManagedUser,
  Me,
  PermissionInfo,
  Role,
  RoleInput,
  TokenPair,
  UploadResponse,
  UserCreateInput,
  UserUpdateInput,
} from '../types'
import { clearTokens, getTokens, notifySessionLost, setTokens } from '../auth/session'

export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api'

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
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

let refreshing: Promise<boolean> | null = null

/** Exchange the refresh token for a new pair. Concurrent callers share one attempt. */
function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing
  const tokens = getTokens()
  if (!tokens) return Promise.resolve(false)
  refreshing = (async () => {
    try {
      const res = await fetch(`${BASE_URL}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: tokens.refresh }),
      })
      if (!res.ok) return false
      const pair = (await res.json()) as TokenPair
      setTokens({ access: pair.access_token, refresh: pair.refresh_token })
      return true
    } catch {
      return false
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

async function send(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  const tokens = getTokens()
  if (tokens) headers.set('Authorization', `Bearer ${tokens.access}`)
  if (typeof init.body === 'string') headers.set('Content-Type', 'application/json')
  try {
    return await fetch(`${BASE_URL}${path}`, { ...init, headers })
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : 'Network error')
  }
}

/** The single request path: every API call goes through here, so errors are uniform.
 * A 401 triggers one token refresh and one retry; if that fails the session is dropped. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await send(path, init)
  if (res.status === 401 && !path.startsWith('/auth/') && getTokens()) {
    if (await refreshSession()) {
      res = await send(path, init)
    } else {
      clearTokens()
      notifySessionLost()
    }
  }
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  login: (email: string, password: string) =>
    request<TokenPair>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<Me>('/auth/me'),
  listPermissions: () => request<PermissionInfo[]>('/permissions'),
  listUsers: () => request<ManagedUser[]>('/users'),
  createUser: (input: UserCreateInput) =>
    request<ManagedUser>('/users', { method: 'POST', body: JSON.stringify(input) }),
  updateUser: (id: string, input: UserUpdateInput) =>
    request<ManagedUser>(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),
  deleteUser: (id: string) => request<void>(`/users/${id}`, { method: 'DELETE' }),
  listRoles: () => request<Role[]>('/roles'),
  createRole: (input: RoleInput) =>
    request<Role>('/roles', { method: 'POST', body: JSON.stringify(input) }),
  updateRole: (id: string, input: RoleInput) =>
    request<Role>(`/roles/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),
  deleteRole: (id: string) => request<void>(`/roles/${id}`, { method: 'DELETE' }),
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
