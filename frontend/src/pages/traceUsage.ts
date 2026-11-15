import type { Trace } from '../types'

/** What a Traces table cell shows and why (the title is the hover/AT explanation). */
export interface UsageCell {
  text: string
  title?: string
  /** `known` = a real number the provider reported; the rest are the three kinds of "no number". */
  kind: 'known' | 'cached' | 'unavailable' | 'none'
}

const rec = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {}

/** Served from the answer cache (chat) or the embedding/retrieval cache (search): the provider was not called. */
export function isCached(t: Trace): boolean {
  const cache = rec(t.meta.cache)
  if (t.kind === 'chat') return cache.answer === 'hit'
  if (t.kind === 'search') return rec(t.meta.embedding).cached === true || cache.result === 'hit'
  return false
}

/** Why a trace has no usage number: not applicable (`—`) versus expected but not reported (`n/a`). */
function missing(t: Trace): UsageCell {
  if (isCached(t)) {
    return {
      text: 'cached',
      title: 'Served from the cache: no provider call was made, so nothing was spent.',
      kind: 'cached',
    }
  }
  if (t.status === 'error' || (t.kind === 'search' && t.meta.mode === 'keyword')) {
    return {
      text: '—',
      title:
        t.status === 'error'
          ? 'The request failed and the provider reported no usage.'
          : 'Keyword search does not call an embedding model.',
      kind: 'none',
    }
  }
  return {
    text: 'n/a',
    title:
      'No usage was recorded: the provider returned no count (or the trace predates usage capture).',
    kind: 'unavailable',
  }
}

export function tokensCell(t: Trace): UsageCell {
  if (isCached(t) && (t.prompt_tokens ?? 0) === 0 && (t.completion_tokens ?? 0) === 0) {
    return missing(t)
  }
  if (t.prompt_tokens === null && t.completion_tokens === null) return missing(t)
  const inTok = t.prompt_tokens ?? 0
  const outTok = t.completion_tokens ?? 0
  return {
    text: `${inTok.toLocaleString()} / ${outTok.toLocaleString()}`,
    title: t.kind === 'chat' ? 'Prompt / completion tokens' : 'Tokens embedded by the provider',
    kind: 'known',
  }
}

/** USD with no scientific notation and no trailing zeros ($0, $0.0123, $0.00000068). */
export function formatUsd(v: number): string {
  if (v === 0) return '$0'
  return '$' + v.toFixed(8).replace(/0+$/, '').replace(/\.$/, '')
}

export function costCell(t: Trace): UsageCell {
  if (t.cost_usd === null) {
    const c = missing(t)
    if (c.kind === 'unavailable') {
      return {
        ...c,
        title:
          'Cost unknown: the provider reported none and no price is configured (LLM_PRICE_* / EMBEDDING_PRICE_PER_MTOK).',
      }
    }
    return c
  }
  return {
    text: formatUsd(t.cost_usd),
    title:
      t.cost_usd === 0 ? 'Free: a local model or a free tier.' : 'Cost reported by the provider',
    kind: 'known',
  }
}
