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
  /** Present for documents ingested from a link: the URL (query string removed) and size fetched. */
  source_url?: string
  bytes_downloaded?: number
  storage?: string
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
  /** Link the document was ingested from (credentials/query removed); null for uploads. */
  source_url?: string | null
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
  /** Null for context chunks (`is_hit` false): they are not scored and never borrow their hit's score. */
  score: number | null
  /** Absent on older servers (then `score` is a distance). Scores are not comparable across kinds. */
  score_kind?: ScoreKind | (string & {})
  /** Hybrid only: 1-based rank in the semantic / keyword candidate list, null when absent there. */
  semantic_rank?: number | null
  keyword_rank?: number | null
  /** False for neighbouring context chunks (absent on older servers: treat as a hit). */
  is_hit?: boolean
  /** 1-based position among the hits, best first; null/absent for context chunks. */
  rank?: number | null
  /** Context chunks only: the hit (document_id, chunk_index) this chunk accompanies. */
  context_for?: { document_id: string; chunk_index: number } | null
}

export interface ChatResponse {
  answer: string
  sources: Source[]
}

/** Final event of `POST /chat/stream`. */
export interface ChatDone {
  answer: string
  usage: { prompt_tokens: number | null; completion_tokens: number | null }
  cost_usd: number | null
  cache: string
  trace_id: string | null
}

export type ChatStreamEvent =
  | { event: 'sources'; data: { sources: Source[]; cache: string } }
  | { event: 'token'; data: { text: string } }
  | { event: 'done'; data: ChatDone }
  | { event: 'error'; data: { status: number; detail: string; trace_id: string | null } }

export interface HealthCheck {
  ok: boolean
  detail: string
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  /** Public flag: false when the server runs with AUTH_ENABLED=false (the dashboard then skips login). */
  auth_enabled?: boolean
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

export interface ApiKey {
  id: string
  name: string
  prefix: string
  role_id: string
  role: string
  owner_email: string
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  status: 'active' | 'expired' | 'revoked'
}

/** Returned once by POST /api-keys: `key` is the only time the secret is available. */
export interface ApiKeyCreated extends ApiKey {
  key: string
  warnings: string[]
}

export interface ApiKeyCreateInput {
  name: string
  role_id: string
  expires_in_days?: number
}

export interface PermissionInfo {
  name: string
  description: string
}

export interface Trace {
  id: string
  created_at: string
  kind: 'chat' | 'search' | 'ingest'
  status: 'ok' | 'error'
  duration_ms: number
  user_email: string | null
  document_id: string | null
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  hits: number | null
  cost_usd: number | null
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
  /** Sum of the known costs of the day; null = none known (not 0). */
  cost_usd: number | null
  /** Successful chat requests of the day whose cost is unknown. */
  unpriced_requests: number
}

export interface ModelCost {
  model: string | null
  requests: number
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number | null
  unpriced_requests: number
}
export interface Bucket {
  lo: number
  hi: number
  count: number
}
export interface Reason {
  reason: string
  count: number
}
export interface HeatCell {
  /** 0 = Monday ... 6 = Sunday (UTC) */
  weekday: number
  hour: number
  count: number
}
export interface CitedDocument {
  document_id: string
  filename: string | null
  citations: number
}

export interface Insights {
  days: number
  kind: string | null
  cost_usd: number | null
  priced_chats: number
  unpriced_chats: number
  avg_cost_per_chat: number | null
  by_model: ModelCost[]
  latency_bucket_ms: number
  latency_histogram: Bucket[]
  errors_by_reason: Reason[]
  heatmap: HeatCell[]
  rag_days: { day: string; queries: number; no_hit: number }[]
  hits_histogram: { hits: number; count: number }[]
  top_documents: CitedDocument[]
  cited_documents: number
  total_documents: number
  never_cited: number
  reuse_days: { day: string; ingests: number; reused: number; embedded: number }[]
  ingest_modes: { mode: string; runs: number; avg_ms: number; p95_ms: number }[]
  ingest_failures: Reason[]
}

export interface Credits {
  available: boolean
  reason: 'provider_not_openrouter' | 'no_api_key' | 'unavailable' | null
  limit: number | null
  limit_remaining: number | null
  usage: number | null
  is_free_tier: boolean | null
  fetched_at: number | null
}

export interface CacheKindStats {
  requests: number
  with_cache_info: number
  hits: number
  misses: number
  bypassed: number
  hit_rate: number | null
  avg_hit_ms: number | null
  avg_miss_ms: number | null
  layers: Record<string, { hit: number; miss: number }>
  llm_calls_avoided: number
  embeddings_avoided: number
  saved_usd: number | null
}

export interface CacheStats {
  config: { enabled: boolean; redis?: string; ttl_seconds?: Record<string, number> }
  process: { enabled: boolean; breaker_open: boolean; last_error: string | null }
  redis: {
    reachable: boolean
    keys: Record<string, number>
    index_bumps?: number | null
    used_memory_bytes?: number
    error?: string
  }
  traces: { search: CacheKindStats; chat: CacheKindStats }
}

// ── Settings (ticket 43) ─────────────────────────────────────────────────────
export type SettingApply = 'live' | 'restart' | 'reindex' | 'bootstrap'
export type SettingSource = 'env' | 'ui' | 'config' | 'default'
export type SettingKind = 'string' | 'text' | 'integer' | 'number' | 'boolean' | 'enum' | 'path'
export type SettingValue = string | number | boolean | null

export interface SettingItem {
  key: string
  group: string
  description: string
  kind: SettingKind
  nullable: boolean
  choices: string[] | null
  unit: string | null
  note: string | null
  apply: SettingApply
  secret: boolean
  reindex_action: 'reindex' | 'rebuild_fts' | null
  /** Effective value; always null for secrets. */
  value: SettingValue
  /** Secrets only: is a value in force? */
  is_set: boolean | null
  default: SettingValue
  source: SettingSource
  locked: boolean
  locked_reason: string | null
  disabled_reason: string | null
  shadowed: boolean
  shadowed_value: SettingValue
  updated_at: string | null
  updated_by: string | null
  restart_pending: boolean
  undecryptable: boolean
}

export interface SettingsView {
  groups: { id: string; description: string }[]
  settings: SettingItem[]
  summary: {
    secrets_enabled: boolean
    restart_pending: string[]
    overrides_error: string | null
    undecryptable: string[]
    refresh_seconds: number
    missing: { area: string; message: string }[]
  }
}

export interface SettingsPatchInput {
  set: Record<string, SettingValue>
  reset: string[]
  confirm_reindex?: boolean
  dry_run?: boolean
}

export interface SettingsPatchResult {
  changed: string[]
  reset: string[]
  unchanged: string[]
  restart_required: string[]
  reindex_required: string[]
  dry_run: boolean
  reindex: { queued: boolean; kinds: string[]; task_ids: string[]; error: string | null } | null
  settings: SettingItem[]
}

export interface SettingsAuditRow {
  id: string
  at: string
  actor: string
  action: string
  key: string
  old_value: SettingValue
  new_value: SettingValue
  secret: boolean
  detail: Record<string, unknown> | null
}

export interface ReindexStatus {
  state: 'idle' | 'pending' | 'running' | 'done' | 'failed' | 'unknown'
  needed: boolean
  problems: string[]
  queued_at: string | null
  queued_by: string | null
  kind: string | null
  task_id: string | null
  result: Record<string, unknown> | null
  error: string | null
}

export interface ConnectionTest {
  service: string
  ok: boolean
  detail: string
  provider: string
  latency_ms: number
}
