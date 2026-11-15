import type {
  ApiKey,
  ApiKeyCreated,
  ApiKeyCreateInput,
  ChatRequest,
  ChatResponse,
  ChatStreamEvent,
  Chunk,
  CacheStats,
  ConnectionTest,
  Credits,
  DayUsage,
  Document,
  DocumentDetail,
  HealthResponse,
  Insights,
  Job,
  ManagedUser,
  ReindexStatus,
  SettingsAuditRow,
  SettingsPatchInput,
  SettingsPatchResult,
  SettingsView,
  Me,
  PermissionInfo,
  Role,
  RoleInput,
  TokenPair,
  Trace,
  TracePage,
  UploadResponse,
  UserCreateInput,
  UserUpdateInput,
} from '../types'
import { clearTokens, getTokens, notifySessionLost, setTokens } from '../auth/session'
import { StreamUnavailableError, readChatStream } from './stream'

export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api'

export class ApiError extends Error {
  readonly status: number
  /** The raw `detail` of the error body (e.g. per-field validation errors), when there was one. */
  readonly detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** FastAPI 422 detail (a list of {loc, msg}) as one readable line, or null if it isn't that shape. */
function validationMessage(detail: unknown): string | null {
  if (!Array.isArray(detail) || detail.length === 0) return null
  const parts = detail.map((d: unknown) => {
    if (!d || typeof d !== 'object' || !('msg' in d) || typeof d.msg !== 'string') return null
    const loc =
      'loc' in d && Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body').join('.') : ''
    return loc ? `${loc}: ${d.msg}` : d.msg
  })
  return parts.every((p) => p !== null) ? parts.join('; ') : null
}

async function errorInfo(res: Response): Promise<{ message: string; detail: unknown }> {
  try {
    const body: unknown = await res.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return { message: detail, detail }
      if (
        detail &&
        typeof detail === 'object' &&
        'message' in detail &&
        typeof (detail as { message: unknown }).message === 'string'
      )
        return { message: (detail as { message: string }).message, detail }
      return { message: validationMessage(detail) ?? JSON.stringify(detail), detail }
    }
  } catch {
    /* non-JSON error body */
  }
  return { message: res.statusText || `HTTP ${res.status}`, detail: undefined }
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
    if (typeof e === 'object' && e !== null && (e as { name?: unknown }).name === 'AbortError')
      throw e // a deliberate cancel, not a network failure
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : 'Network error')
  }
}

/** Authenticated fetch. A 401 triggers one token refresh and one retry; if that fails the session is dropped. */
async function sendAuthed(path: string, init: RequestInit): Promise<Response> {
  let res = await send(path, init)
  if (res.status === 401 && !path.startsWith('/auth/') && getTokens()) {
    if (await refreshSession()) {
      res = await send(path, init)
    } else {
      clearTokens()
      notifySessionLost()
    }
  }
  return res
}

/** The single request path for JSON calls: every API call goes through here, so errors are uniform. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await sendAuthed(path, init)
  if (!res.ok) {
    const info = await errorInfo(res)
    throw new ApiError(res.status, info.message, info.detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** Open `POST /chat/stream` and feed its events to `onEvent`. Resolves when the stream ends (normally after a
 * `done` or `error` event); rejects with `StreamUnavailableError` when it could not be opened (then use `chat`),
 * `ApiError` for a real HTTP error, and an `AbortError` when `signal` fires. */
async function chatStream(
  req: ChatRequest,
  onEvent: (e: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await sendAuthed('/chat/stream', {
      method: 'POST',
      body: JSON.stringify(req),
      headers: { Accept: 'text/event-stream' },
      signal,
    })
  } catch (e) {
    if (e instanceof ApiError && e.status === 0) throw new StreamUnavailableError(e.message)
    throw e
  }
  if (!res.ok) {
    // an older backend (or a proxy) without the route: use the plain endpoint instead
    if ([404, 405, 501].includes(res.status))
      throw new StreamUnavailableError(`streaming is not available (HTTP ${res.status})`)
    throw new ApiError(res.status, (await errorInfo(res)).message)
  }
  if (!res.body || !(res.headers.get('Content-Type') ?? '').includes('text/event-stream'))
    throw new StreamUnavailableError('the server did not answer with an event stream')
  await readChatStream(res.body, onEvent)
}

export const api = {
  health: () => request<HealthResponse>('/health'),
  login: (email: string, password: string) =>
    request<TokenPair>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<Me>('/auth/me'),
  listTraces: (
    params: { kind?: string; status?: string; limit?: number; offset?: number } = {},
  ) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params))
      if (v !== undefined && v !== '') q.set(k, String(v))
    const qs = q.toString()
    return request<TracePage>(`/traces${qs ? `?${qs}` : ''}`)
  },
  getTrace: (id: string) => request<Trace>(`/traces/${id}`),
  usage: (days = 14, kind = '') =>
    request<DayUsage[]>(`/usage?days=${days}${kind ? `&kind=${kind}` : ''}`),
  usageInsights: (days = 14, kind = '') =>
    request<Insights>(`/usage/insights?days=${days}${kind ? `&kind=${kind}` : ''}`),
  usageCredits: () => request<Credits>('/usage/credits'),
  cacheStats: (days = 7) => request<CacheStats>(`/cache/stats?days=${days}`),
  listPermissions: () => request<PermissionInfo[]>('/permissions'),
  listUsers: () => request<ManagedUser[]>('/users'),
  createUser: (input: UserCreateInput) =>
    request<ManagedUser>('/users', { method: 'POST', body: JSON.stringify(input) }),
  updateUser: (id: string, input: UserUpdateInput) =>
    request<ManagedUser>(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(input) }),
  deleteUser: (id: string) => request<void>(`/users/${id}`, { method: 'DELETE' }),
  listApiKeys: () => request<ApiKey[]>('/api-keys'),
  createApiKey: (input: ApiKeyCreateInput) =>
    request<ApiKeyCreated>('/api-keys', { method: 'POST', body: JSON.stringify(input) }),
  revokeApiKey: (id: string) => request<ApiKey>(`/api-keys/${id}/revoke`, { method: 'POST' }),
  getSettings: () => request<SettingsView>('/settings'),
  patchSettings: (input: SettingsPatchInput) =>
    request<SettingsPatchResult>('/settings', { method: 'PATCH', body: JSON.stringify(input) }),
  settingsAudit: (before?: string) =>
    request<SettingsAuditRow[]>(
      `/settings/audit?limit=100${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ),
  reindexStatus: () => request<ReindexStatus>('/settings/reindex'),
  queueReindex: (kind: 'reindex' | 'rebuild_fts' = 'reindex') =>
    request<{ queued: boolean; error: string | null }>('/settings/reindex', {
      method: 'POST',
      body: JSON.stringify({ confirm_reindex: true, kind }),
    }),
  testConnection: (service: string) =>
    request<ConnectionTest>(`/settings/test/${service}`, { method: 'POST' }),
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
  ingestUrl: (url: string) =>
    request<UploadResponse>('/documents/from-url', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  replace: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<UploadResponse>(`/documents/${id}`, { method: 'PUT', body: form })
  },
  chatStream,
  chat: (req: ChatRequest) =>
    request<ChatResponse>('/chat', { method: 'POST', body: JSON.stringify(req) }),
}
