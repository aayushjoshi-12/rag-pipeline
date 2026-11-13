// Mirrors backend pydantic schemas (backend/app/schema/*.py). Keep in sync by hand.

export type DocumentStatus = 'pending' | 'ingesting' | 'ready' | 'failed'
export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

export interface Job {
  id: string
  document_id: string
  task_id: string | null
  status: JobStatus
  error: string | null
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
