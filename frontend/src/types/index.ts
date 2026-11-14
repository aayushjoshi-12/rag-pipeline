// Mirrors backend pydantic schemas (backend/app/schema/*.py). Keep in sync by hand.

export type DocumentStatus = 'pending' | 'ingesting' | 'ready' | 'failed'
export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

export interface JobStats {
  mode: 'full' | 'incremental'
  reason: string
  chunks_total: number
  kept: number
  added: number
  removed: number
  embedded: number
}

export interface Job {
  id: string
  document_id: string
  task_id: string | null
  status: JobStatus
  error: string | null
  stats: JobStats | null
  created_at: string
  updated_at: string
}

export interface Document {
  id: string
  filename: string
  content_type: string
  status: DocumentStatus
  created_at: string
  updated_at: string
}

export interface DocumentDetail extends Document {
  jobs: Job[]
}

export interface UploadResponse {
  document: Document
  job: Job
}

export interface Chunk {
  id: string
  index: number
  text: string
  metadata: Record<string, unknown>
}

export type RetrievalMode = 'semantic' | 'keyword' | 'hybrid'
export type RetrievalFusion = 'rrf' | 'weighted'

export interface ChatRequest {
  question: string
  document_ids?: string[]
  top_k?: number
  /** Per-request override; omit to use the server default. */
  mode?: RetrievalMode
  /** Per-request override, only meaningful for hybrid; omit to use the server default. */
  fusion?: RetrievalFusion
}

/** What `score` means: `distance` is lower-is-closer, every other kind is higher-is-better. */
export type ScoreKind = 'distance' | 'bm25' | 'ts_rank_cd' | 'rrf' | 'weighted'

export interface Source {
  text: string
  document_id: string
  chunk_index: number
  filename: string
  score: number
  /** Absent on older servers (then `score` is a distance). Scores are not comparable across kinds. */
  score_kind?: ScoreKind | (string & {})
  /** Hybrid only: 1-based rank in the semantic / keyword candidate list, null when absent there. */
  semantic_rank?: number | null
  keyword_rank?: number | null
  /** False for neighbouring context chunks (absent on older servers: treat as a hit). */
  is_hit?: boolean
}

export interface ChatResponse {
  answer: string
  sources: Source[]
}

export interface HealthCheck {
  ok: boolean
  detail: string
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  checks: Record<string, HealthCheck>
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface Me {
  id: string | null
  email: string
  permissions: string[]
  auth_enabled: boolean
}

export interface Role {
  id: string
  name: string
  description: string
  is_builtin: boolean
  permissions: string[]
}

export interface RoleInput {
  name?: string
  description?: string
  permissions?: string[]
}

export interface ManagedUser {
  id: string
  email: string
  is_active: boolean
  roles: string[]
  permissions: string[]
}

export interface UserCreateInput {
  email: string
  password: string
  role_ids: string[]
  is_active?: boolean
}

export interface UserUpdateInput {
  password?: string
  is_active?: boolean
  role_ids?: string[]
}

export interface PermissionInfo {
  name: string
  description: string
}

export interface Trace {
  id: string
  created_at: string
  kind: 'chat' | 'ingest'
  status: 'ok' | 'error'
  duration_ms: number
  user_email: string | null
  document_id: string | null
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  hits: number | null
  error: string | null
  question: string | null
  meta: Record<string, unknown>
}

export interface TracePage {
  items: Trace[]
  total: number
}

export interface DayUsage {
  day: string
  requests: number
  errors: number
  avg_ms: number | null
  p95_ms: number | null
  prompt_tokens: number
  completion_tokens: number
}
