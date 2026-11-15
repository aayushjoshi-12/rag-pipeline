import type { ChatRequest, RetrievalFusion, RetrievalMode, Source } from './types'

/** '' means "Server default": the field is left out of the request. */
export interface RetrievalPrefs {
  mode: '' | RetrievalMode
  fusion: '' | RetrievalFusion
  /** Passages to retrieve (1-50); '' = server default. Kept as text so the input can be empty. */
  topK?: string
  /** Neighbouring chunks added around each hit (0-5); '' = server default. */
  neighbors?: string
}

export const TOP_K_MAX = 50
export const NEIGHBORS_MAX = 5

export const DEFAULT_PREFS: RetrievalPrefs = { mode: '', fusion: '', topK: '', neighbors: '' }
export const STORAGE_KEY = 'rag.retrieval'

const MODES: readonly string[] = ['', 'semantic', 'keyword', 'hybrid']
const FUSIONS: readonly string[] = ['', 'rrf', 'weighted']

export const MODE_HELP: Record<RetrievalPrefs['mode'], string> = {
  '': 'Uses the mode the server is configured with.',
  semantic: 'Matches by meaning. Best for paraphrases and conceptual questions.',
  keyword: 'Matches exact words, section numbers and IDs.',
  hybrid: 'Combines meaning and exact words, then merges the two rankings.',
}
export const FUSION_HELP: Record<RetrievalPrefs['fusion'], string> = {
  '': 'Uses the fusion method the server is configured with.',
  rrf: 'Reciprocal rank fusion: rewards chunks that rank well in both lists.',
  weighted: 'Weighted blend of normalised scores; lets one retriever dominate.',
}

/** A whole number within [min, max] as text, otherwise '' (= server default). */
export function intOrEmpty(v: unknown, min: number, max: number): string {
  if (typeof v !== 'string' && typeof v !== 'number') return ''
  const s = String(v).trim()
  if (!/^\d{1,3}$/.test(s)) return ''
  const n = Number(s)
  return n >= min && n <= max ? String(n) : ''
}

export function loadPrefs(): RetrievalPrefs {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_PREFS
    const p = JSON.parse(raw) as Partial<Record<keyof RetrievalPrefs, unknown>>
    const mode = typeof p.mode === 'string' && MODES.includes(p.mode) ? p.mode : ''
    const fusion = typeof p.fusion === 'string' && FUSIONS.includes(p.fusion) ? p.fusion : ''
    return {
      mode,
      fusion,
      topK: intOrEmpty(p.topK, 1, TOP_K_MAX),
      neighbors: intOrEmpty(p.neighbors, 0, NEIGHBORS_MAX),
    } as RetrievalPrefs
  } catch {
    return DEFAULT_PREFS // storage unavailable or corrupt: behave as if nothing was saved
  }
}

export function savePrefs(prefs: RetrievalPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs))
  } catch {
    /* storage unavailable (private window, blocked): the choice just isn't remembered */
  }
}

/** Only the fields that differ from "Server default"; fusion only applies to hybrid. */
export type RequestOverrides = Pick<ChatRequest, 'mode' | 'fusion' | 'top_k' | 'neighbors'>

export function requestOverrides(prefs: RetrievalPrefs): RequestOverrides {
  const topK = intOrEmpty(prefs.topK, 1, TOP_K_MAX)
  const neighbors = intOrEmpty(prefs.neighbors, 0, NEIGHBORS_MAX)
  return {
    ...(prefs.mode ? { mode: prefs.mode } : {}),
    ...(prefs.mode === 'hybrid' && prefs.fusion ? { fusion: prefs.fusion } : {}),
    ...(topK ? { top_k: Number(topK) } : {}),
    ...(neighbors ? { neighbors: Number(neighbors) } : {}),
  }
}

/** What differs from the server defaults, as short parts ("Hybrid · RRF", "top 8", "±2"). */
export function overrideParts(prefs: RetrievalPrefs): string[] {
  const o = requestOverrides(prefs)
  const parts: string[] = []
  if (o.mode) {
    const name = o.mode[0]!.toUpperCase() + o.mode.slice(1)
    parts.push(o.fusion ? `${name} · ${o.fusion.toUpperCase()}` : name)
  }
  if (o.top_k !== undefined) parts.push(`top ${o.top_k}`)
  if (o.neighbors !== undefined) parts.push(`±${o.neighbors} neighbours`)
  return parts
}

export function prefsSummary(prefs: RetrievalPrefs): string {
  const parts = overrideParts(prefs)
  return parts.length ? parts.join(' · ') : 'Server default'
}

export interface ScoreLabel {
  text: string
  title: string
}

const HIGHER = 'higher is better'
const LOWER = 'Distance to the question: lower is closer.'
const NOT_COMPARABLE = 'Scores are not comparable between retrieval modes.'

/** A human label for a source score; never a bare number, always names its scale. */
export function scoreLabel(source: Pick<Source, 'score' | 'score_kind'>): ScoreLabel {
  const s = source.score
  if (s == null) {
    return { text: 'not scored', title: 'Neighbouring context chunk: it was not ranked or scored.' }
  }
  switch (source.score_kind ?? 'distance') {
    case 'distance':
      return { text: `distance ${s.toFixed(2)}`, title: LOWER }
    case 'bm25':
      return {
        text: `BM25 ${s.toFixed(1)}`,
        title: `BM25 keyword relevance: ${HIGHER}. ${NOT_COMPARABLE}`,
      }
    case 'ts_rank_cd':
      return {
        text: `ts_rank_cd ${s.toFixed(2)}`,
        title: `Postgres full-text rank: ${HIGHER}. ${NOT_COMPARABLE}`,
      }
    case 'rrf':
      return {
        text: `RRF ${s.toFixed(3)}`,
        title: `Reciprocal rank fusion score: ${HIGHER}. ${NOT_COMPARABLE}`,
      }
    case 'weighted':
      return {
        text: `weighted ${s.toFixed(3)}`,
        title: `Weighted fusion score: ${HIGHER}. ${NOT_COMPARABLE}`,
      }
    default:
      return {
        text: `score ${s.toFixed(2)}`,
        title: `Unrecognised score type "${String(source.score_kind)}"; its scale is unknown. ${NOT_COMPARABLE}`,
      }
  }
}
