import { describe, expect, it } from 'vitest'
import type { Trace } from '../types'
import { costCell, formatUsd, isCached, tokensCell } from './traceUsage'

const t = (over: Partial<Trace> = {}): Trace => ({
  id: 'x',
  created_at: '2026-11-15T10:00:00',
  kind: 'chat',
  status: 'ok',
  duration_ms: 10,
  user_email: null,
  document_id: null,
  model: 'm',
  prompt_tokens: 120,
  completion_tokens: 9,
  hits: 1,
  cost_usd: 0.0000123,
  error: null,
  question: null,
  meta: {},
  ...over,
})

describe('trace usage cells', () => {
  it('shows real numbers with an explanatory title', () => {
    expect(tokensCell(t())).toMatchObject({ text: '120 / 9', kind: 'known' })
    expect(
      tokensCell(t({ prompt_tokens: 1234, completion_tokens: 0, kind: 'search' })),
    ).toMatchObject({
      text: '1,234 / 0',
      kind: 'known',
    })
  })

  it('answer-cache hits are "cached", not unknown, for tokens and cost', () => {
    const hit = t({
      prompt_tokens: 0,
      completion_tokens: 0,
      cost_usd: 0,
      meta: { cache: { answer: 'hit' } },
    })
    expect(isCached(hit)).toBe(true)
    expect(tokensCell(hit)).toMatchObject({ text: 'cached', kind: 'cached' })
    expect(tokensCell(hit).title).toMatch(/cache/)
    // the money is a real $0
    expect(costCell(hit)).toMatchObject({ text: '$0', kind: 'known' })
  })

  it('a chat that missed the answer cache is not "cached"', () => {
    expect(isCached(t({ meta: { cache: { result: 'miss', answer: 'miss' } } }))).toBe(false)
  })

  it('search served from the embedding or retrieval cache is cached', () => {
    const embed = t({
      kind: 'search',
      prompt_tokens: 0,
      completion_tokens: 0,
      meta: { embedding: { tokens: 0, cached: true } },
    })
    expect(tokensCell(embed).kind).toBe('cached')
    expect(
      isCached(
        t({
          kind: 'search',
          prompt_tokens: null,
          completion_tokens: null,
          meta: { cache: { result: 'hit' } },
        }),
      ),
    ).toBe(true)
  })

  it('"n/a" = ok but the provider gave no count; "—" = failed or not applicable', () => {
    const na = t({ prompt_tokens: null, completion_tokens: null, cost_usd: null })
    expect(tokensCell(na)).toMatchObject({ text: 'n/a', kind: 'unavailable' })
    expect(tokensCell(na).title).toMatch(/provider returned no count/)
    expect(costCell(na)).toMatchObject({ text: 'n/a' })
    expect(costCell(na).title).toMatch(/price/)
    const failed = t({
      status: 'error',
      prompt_tokens: null,
      completion_tokens: null,
      cost_usd: null,
    })
    expect(tokensCell(failed)).toMatchObject({ text: '—', kind: 'none' })
    expect(costCell(failed).text).toBe('—')
    const keyword = t({
      kind: 'search',
      prompt_tokens: null,
      completion_tokens: null,
      cost_usd: null,
      meta: { mode: 'keyword' },
    })
    expect(tokensCell(keyword).text).toBe('—')
  })

  it('a failed call that kept its usage shows the real numbers', () => {
    const f = t({
      status: 'error',
      prompt_tokens: 2500,
      completion_tokens: 2048,
      cost_usd: 0.00011,
    })
    expect(tokensCell(f)).toMatchObject({ text: '2,500 / 2,048', kind: 'known' })
    expect(costCell(f).text).toBe('$0.00011')
  })

  it('formats dollars without exponents or trailing zeros', () => {
    expect(formatUsd(0)).toBe('$0')
    expect(formatUsd(0.00000068)).toBe('$0.00000068')
    expect(formatUsd(0.0123)).toBe('$0.0123')
    expect(formatUsd(1.5)).toBe('$1.5')
    expect(costCell(t({ cost_usd: 0 })).title).toMatch(/Free/)
  })
})
