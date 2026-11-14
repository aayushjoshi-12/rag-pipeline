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

export interface ChatRequest {
  question: string
  document_ids?: string[]
  top_k?: number
}

export interface Source {
  text: string
  document_id: string
  chunk_index: number
  filename: string
  score: number
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
