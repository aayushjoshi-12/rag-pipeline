import '@testing-library/jest-dom/vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { baseInsights, dayUsage, renderAt, resetApp } from '../test/helpers'
import type { Credits } from '../types'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    usage: vi.fn(),
    usageInsights: vi.fn(),
    usageCredits: vi.fn(),
    cacheStats: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))
const m = vi.mocked(api)

const day = dayUsage
const insights = baseInsights
const credits = (over: Partial<Credits> = {}): Credits => ({
  available: true,
  reason: null,
  limit: 2.5,
  limit_remaining: 2.4,
  usage: 0.1,
  is_free_tier: false,
  fetched_at: 1,
  ...over,
})

beforeEach(() => {
  resetApp(m)
  m.usage.mockResolvedValue([day()])
  m.usageInsights.mockResolvedValue(insights())
  m.usageCredits.mockResolvedValue(credits())
  m.cacheStats.mockRejectedValue(new Error('not stubbed'))
})

describe('Usage tabs', () => {
  it('opens on Cost, shows one panel at a time and switches with a click', async () => {
    m.usage.mockResolvedValue([day({ cost_usd: 0.01 })])
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Cost',
      'Performance',
      'RAG health',
      'Ingest',
      'Cache',
    ])
    expect(screen.getByRole('tab', { name: 'Cost' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1)
    expect(screen.getByRole('tabpanel')).toHaveAccessibleName('Cost')
    expect(screen.getByRole('img', { name: 'Spend per day' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('tab', { name: 'Performance' }))
    expect(screen.getByRole('tab', { name: 'Performance' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.queryByRole('img', { name: 'Spend per day' })).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Latency per day' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('tab', { name: 'RAG health' }))
    expect(await screen.findByTestId('tile-never-cited')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Ingest' }))
    expect(await screen.findByText('No successful ingests in this period.')).toBeInTheDocument()
  })

  it('is a keyboard tablist: arrows move and activate, Home/End jump, one Tab stop', async () => {
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    const [cost, perf, rag, ingest, cache] = screen.getAllByRole('tab')
    expect([cost, perf, rag, ingest, cache].map((t) => t!.getAttribute('tabindex'))).toEqual([
      '0',
      '-1',
      '-1',
      '-1',
      '-1',
    ])
    cost!.focus()
    await userEvent.keyboard('{ArrowRight}')
    expect(perf).toHaveAttribute('aria-selected', 'true')
    expect(document.activeElement).toBe(perf)
    await userEvent.keyboard('{End}')
    expect(cache).toHaveAttribute('aria-selected', 'true')
    await userEvent.keyboard('{ArrowRight}') // wraps to the first
    expect(cost).toHaveAttribute('aria-selected', 'true')
    await userEvent.keyboard('{ArrowLeft}') // wraps to the last
    expect(cache).toHaveAttribute('aria-selected', 'true')
    await userEvent.keyboard('{Home}')
    expect(cost).toHaveAttribute('aria-selected', 'true')
  })

  it('opens the tab named in the URL and ignores unknown names', async () => {
    const { unmount } = renderAt('/usage?tab=rag')
    expect(await screen.findByTestId('tile-never-cited')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'RAG health' })).toHaveAttribute('aria-selected', 'true')
    unmount()
    renderAt('/usage?tab=bogus')
    await screen.findByTestId('tile-requests')
    expect(screen.getByRole('tab', { name: 'Cost' })).toHaveAttribute('aria-selected', 'true')
  })

  it('keeps the period and type filters when the tab changes, and loads credits once', async () => {
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    await userEvent.selectOptions(screen.getByLabelText('Period'), '30')
    await userEvent.click(screen.getByRole('tab', { name: 'Performance' }))
    await waitFor(() => expect(m.usageInsights).toHaveBeenLastCalledWith(30, ''))
    expect(m.usage).toHaveBeenLastCalledWith(30, '')
    expect(m.usageCredits).toHaveBeenCalledTimes(1)
  })

  it('re-queries insights together with usage when the type changes', async () => {
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    await userEvent.selectOptions(screen.getByLabelText('Type'), 'ingest')
    await waitFor(() => expect(m.usageInsights).toHaveBeenLastCalledWith(14, 'ingest'))
    expect(m.usage).toHaveBeenLastCalledWith(14, 'ingest')
  })
})

describe('spend tile and cost tab', () => {
  it('sums the known daily costs and flags chats with unknown cost', async () => {
    m.usage.mockResolvedValue([
      day({ day: '2026-11-13', cost_usd: 0.002, unpriced_requests: 1 }),
      day({ day: '2026-11-14', cost_usd: 0.0005, unpriced_requests: 2 }),
    ])
    renderAt('/usage')
    expect(await screen.findByTestId('tile-spend')).toHaveTextContent('$0.0025')
    expect(screen.getByText('3 chats with unknown cost')).toBeInTheDocument()
  })

  it('shows a dash, never $0.00, when no request has a known cost', async () => {
    m.usage.mockResolvedValue([day({ cost_usd: null })])
    renderAt('/usage')
    expect(await screen.findByTestId('tile-spend')).toHaveTextContent('—')
    expect(screen.getByTestId('tile-spend')).not.toHaveTextContent('$0')
    expect(screen.getByText('no cost recorded')).toBeInTheDocument()
    expect(screen.getAllByText(/No cost recorded in this period/, { selector: 'p' })).toHaveLength(
      2,
    )
  })

  it('shows $0.00 for a known zero (free model)', async () => {
    m.usage.mockResolvedValue([day({ cost_usd: 0 })])
    renderAt('/usage')
    expect(await screen.findByTestId('tile-spend')).toHaveTextContent('$0.00')
  })

  it('lists spend by model with tokens and unpriced counts, plus the average per chat', async () => {
    m.usageInsights.mockResolvedValue(
      insights({
        avg_cost_per_chat: 0.0005287,
        by_model: [
          {
            model: 'openai/gpt-4o-mini',
            requests: 36,
            prompt_tokens: 70000,
            completion_tokens: 7200,
            cost_usd: 0.01482,
            unpriced_requests: 0,
          },
          {
            model: 'nvidia/free',
            requests: 72,
            prompt_tokens: 100,
            completion_tokens: 50,
            cost_usd: 0,
            unpriced_requests: 2,
          },
          {
            model: null,
            requests: 3,
            prompt_tokens: 0,
            completion_tokens: 0,
            cost_usd: null,
            unpriced_requests: 0,
          },
        ],
      }),
    )
    renderAt('/usage')
    const list = await screen.findByRole('list', { name: 'Spend by model' })
    expect(within(list).getByText('$0.01482')).toBeInTheDocument()
    expect(within(list).getByText('36 requests · 77.2k tokens')).toBeInTheDocument()
    expect(within(list).getByText('$0.00')).toBeInTheDocument()
    expect(within(list).getByText(/2 unpriced/)).toBeInTheDocument()
    expect(within(list).getByText('unknown model')).toBeInTheDocument()
    expect(within(list).getByText('—')).toBeInTheDocument()
    expect(screen.getByText(/average per answered chat/)).toHaveTextContent('$0.0005287')
  })

  it('shows the insights error inside the model card without breaking the page', async () => {
    m.usage.mockResolvedValue([day({ cost_usd: 0.01 })])
    m.usageInsights.mockRejectedValue(new Error('insights down'))
    renderAt('/usage')
    expect(await screen.findByTestId('tile-requests')).toBeInTheDocument()
    expect(await screen.findByText('insights down')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Spend per day' })).toBeInTheDocument()
  })
})

describe('spend chart tooltip', () => {
  it('says "cost unknown" for a day with requests but no known cost, not $0', async () => {
    m.usage.mockResolvedValue([
      day({ day: '2026-11-13', cost_usd: 0.01 }),
      day({ day: '2026-11-14', cost_usd: null, requests: 4, unpriced_requests: 4 }),
    ])
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    fireEvent.focus(screen.getByLabelText(/Sat, Nov 14: Spend/))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('cost unknown')
    expect(tip).toHaveTextContent('4 unpriced chats')
  })
})

describe('credits budget bar', () => {
  it('shows used vs limit, remaining, and the projected days at the 7-day burn', async () => {
    const days = Array.from({ length: 7 }, (_, i) =>
      day({ day: `2026-11-0${i + 1}`, cost_usd: 0.1 }),
    )
    m.usage.mockResolvedValue(days)
    m.usageCredits.mockResolvedValue(credits({ limit: 2.5, limit_remaining: 2, usage: 0.5 }))
    renderAt('/usage')
    expect(await screen.findByTestId('credits-remaining')).toHaveTextContent('$2.00')
    const meter = screen.getByRole('meter', { name: 'Credits used' })
    expect(meter).toHaveAttribute('aria-valuenow', '0.5')
    expect(meter).toHaveAttribute('aria-valuemax', '2.5')
    expect(meter).toHaveAttribute('aria-valuetext', '$0.50 of $2.50 used')
    expect((meter.firstElementChild as HTMLElement).style.width).toBe('20%')
    // 0.7 spent in 7 days = 0.1/day -> 2.00 / 0.1 = 20 days
    expect(screen.getByTestId('credits-forecast')).toHaveTextContent('About 20 days left')
    expect(screen.getByTestId('credits-forecast')).toHaveTextContent('$0.10 per day')
  })

  it('says the burn is a lower bound when some chats had no known cost', async () => {
    m.usage.mockResolvedValue(
      Array.from({ length: 7 }, (_, i) =>
        day({ day: `2026-11-0${i + 1}`, cost_usd: 0.1, unpriced_requests: i === 0 ? 2 : 0 }),
      ),
    )
    renderAt('/usage')
    expect(await screen.findByTestId('credits-forecast')).toHaveTextContent(
      '2 chats had no known cost, so the burn is a lower bound.',
    )
  })

  it.each([
    ['no priced requests', [day({ cost_usd: null })], 'Projection unavailable'],
    ['no spend', [day({ cost_usd: 0 })], 'not being used up'],
  ])('explains the projection when there is %s in the last 7 days', async (_n, days, text) => {
    m.usage.mockResolvedValue(days)
    renderAt('/usage')
    expect(await screen.findByTestId('credits-forecast')).toHaveTextContent(text)
  })

  it('handles a key without a limit', async () => {
    m.usageCredits.mockResolvedValue(credits({ limit: null, limit_remaining: null, usage: 1.5 }))
    renderAt('/usage')
    expect(await screen.findByTestId('credits-remaining')).toHaveTextContent('$1.50 used')
    expect(screen.queryByRole('meter')).not.toBeInTheDocument()
  })

  it.each([
    ['provider_not_openrouter', /only available for the OpenRouter provider/],
    ['no_api_key', /No OpenRouter API key/],
    ['unavailable', /could not be read/],
  ] as const)('degrades with a reason when credits are unavailable (%s)', async (reason, text) => {
    m.usageCredits.mockResolvedValue(
      credits({ available: false, reason, limit: null, limit_remaining: null, usage: null }),
    )
    renderAt('/usage')
    expect(await screen.findByTestId('credits-unavailable')).toHaveTextContent(text)
    expect(screen.queryByRole('meter')).not.toBeInTheDocument()
  })

  it('degrades when the credits request itself fails', async () => {
    m.usageCredits.mockRejectedValue(new Error('boom'))
    renderAt('/usage')
    expect(await screen.findByTestId('credits-unavailable')).toHaveTextContent(/could not be read/)
    expect(screen.getByTestId('tile-requests')).toBeInTheDocument()
  })
})

describe('performance, RAG and ingest tabs', () => {
  it('shows errors by reason and the latency histogram', async () => {
    m.usageInsights.mockResolvedValue(
      insights({
        latency_bucket_ms: 100,
        latency_histogram: [
          { lo: 0, hi: 100, count: 5 },
          { lo: 100, hi: 200, count: 0 },
          { lo: 200, hi: 300, count: 2 },
        ],
        errors_by_reason: [{ reason: 'LLM provider returned 429', count: 7 }],
        heatmap: [{ weekday: 2, hour: 10, count: 3 }],
      }),
    )
    renderAt('/usage?tab=performance')
    const errs = await screen.findByRole('list', { name: 'Errors by reason' })
    expect(within(errs).getByText('LLM provider returned 429')).toBeInTheDocument()
    expect(within(errs).getByText('7')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Latency distribution' })).toBeInTheDocument()
    expect(screen.getByLabelText('Wed 10:00 UTC: 3 requests')).toBeInTheDocument()
  })

  it('shows an empty state for errors and heatmap on a quiet system', async () => {
    renderAt('/usage?tab=performance')
    expect(await screen.findByText('No errors in this period.')).toBeInTheDocument()
    expect(
      screen.getAllByText('No requests in this period.', { selector: 'p.viz-empty' }).length,
    ).toBeGreaterThanOrEqual(1)
  })

  it('computes the RAG tiles from the insights', async () => {
    m.usageInsights.mockResolvedValue(
      insights({
        rag_days: [
          { day: '2026-11-13', queries: 10, no_hit: 1 },
          { day: '2026-11-14', queries: 30, no_hit: 5 },
        ],
        reuse_days: [
          { day: '2026-11-13', ingests: 1, reused: 40, embedded: 10 },
          { day: '2026-11-14', ingests: 1, reused: 20, embedded: 5 },
        ],
        never_cited: 3,
        total_documents: 8,
        hits_histogram: [
          { hits: 0, count: 6 },
          { hits: 3, count: 34 },
        ],
        top_documents: [
          { document_id: 'a', filename: 'handbook.docx', citations: 104 },
          { document_id: 'b', filename: null, citations: 2 },
        ],
      }),
    )
    renderAt('/usage?tab=rag')
    expect(await screen.findByTestId('tile-queries')).toHaveTextContent('40')
    expect(screen.getByTestId('tile-nohit')).toHaveTextContent('15.0%') // 6 / 40
    expect(screen.getByTestId('tile-never-cited')).toHaveTextContent('3')
    expect(screen.getByText('of 8 in the library')).toBeInTheDocument()
    expect(screen.getByTestId('tile-saved')).toHaveTextContent('60')
    expect(screen.getByText('15 chunks embedded')).toBeInTheDocument()
    const docs = screen.getByRole('list', { name: 'Most-cited documents' })
    expect(within(docs).getByText('handbook.docx')).toBeInTheDocument()
    expect(within(docs).getByText('deleted document')).toBeInTheDocument()
    expect(screen.getByLabelText('3 hits: Queries 34')).toBeInTheDocument()
  })

  it('shows dashes for the no-hit rate without queries and empty states for every RAG chart', async () => {
    renderAt('/usage?tab=rag')
    expect(await screen.findByTestId('tile-nohit')).toHaveTextContent('—')
    expect(
      screen.getByText('No chat answers have used a document in this period.'),
    ).toBeInTheDocument()
    expect(screen.getByText('No chunks were reused in this period.')).toBeInTheDocument()
  })

  it('shows ingest duration by mode and failures by reason', async () => {
    m.usageInsights.mockResolvedValue(
      insights({
        ingest_modes: [
          { mode: 'full', runs: 4, avg_ms: 20000, p95_ms: 38000 },
          { mode: 'incremental', runs: 1, avg_ms: 900, p95_ms: 900 },
        ],
        ingest_failures: [{ reason: 'no text could be extracted from the document', count: 2 }],
      }),
    )
    renderAt('/usage?tab=ingest')
    const modes = await screen.findByRole('list', { name: 'Ingest duration by mode' })
    expect(within(modes).getByText('Full ingest')).toBeInTheDocument()
    expect(within(modes).getByText('20.0 s')).toBeInTheDocument()
    expect(within(modes).getByText('4 runs · p95 38.0 s')).toBeInTheDocument()
    expect(within(modes).getByText('1 run · p95 900 ms')).toBeInTheDocument()
    expect(within(modes).getByText('Incremental update')).toBeInTheDocument()
    const fails = screen.getByRole('list', { name: 'Ingest failures by reason' })
    expect(
      within(fails).getByText('no text could be extracted from the document'),
    ).toBeInTheDocument()
  })

  it('every tab ends with a table view holding the numbers behind its charts', async () => {
    m.usageInsights.mockResolvedValue(
      insights({
        errors_by_reason: [{ reason: 'boom', count: 1 }],
        ingest_modes: [{ mode: 'full', runs: 1, avg_ms: 1, p95_ms: 1 }],
      }),
    )
    const { unmount } = renderAt('/usage?tab=performance')
    await screen.findByText('boom', { selector: '.hbar-label' })
    expect(
      screen.getByRole('table', { name: 'errors by reason', hidden: true }),
    ).toBeInTheDocument()
    unmount()
    renderAt('/usage?tab=ingest')
    expect(
      await screen.findByRole('table', { name: 'ingest duration by mode', hidden: true }),
    ).toBeInTheDocument()
    expect(screen.getByText('Table view')).toBeInTheDocument()
  })
})

describe('Cache tab', () => {
  const kind = (over = {}) => ({
    requests: 10,
    with_cache_info: 10,
    hits: 6,
    misses: 4,
    bypassed: 0,
    hit_rate: 0.6,
    avg_hit_ms: 3,
    avg_miss_ms: 120,
    layers: { retrieval: { hit: 6, miss: 4 } },
    llm_calls_avoided: 0,
    embeddings_avoided: 6,
    saved_usd: null,
    ...over,
  })

  it('shows hit rates, latency and avoided calls from /cache/stats', async () => {
    m.cacheStats.mockResolvedValue({
      config: { enabled: true },
      process: { enabled: true, breaker_open: false, last_error: null },
      redis: { reachable: true, keys: { retrieval: 4, embedding: 3, answer: 0 } },
      traces: { search: kind(), chat: kind({ llm_calls_avoided: 2, saved_usd: 0.004 }) },
    })
    renderAt('/usage?tab=cache')
    expect(await screen.findByTestId('tile-cache-search')).toHaveTextContent('60.0%')
    expect(screen.getByTestId('tile-cache-latency')).toHaveTextContent('3')
    expect(screen.getByTestId('tile-cache-llm')).toHaveTextContent('2')
    expect(m.cacheStats).toHaveBeenCalledWith(14)
  })

  it('warns when Redis is unreachable', async () => {
    m.cacheStats.mockResolvedValue({
      config: { enabled: true },
      process: { enabled: true, breaker_open: true, last_error: 'x' },
      redis: { reachable: false, keys: {}, error: 'refused' },
      traces: { search: kind({ hit_rate: null }), chat: kind({ hit_rate: null }) },
    })
    renderAt('/usage?tab=cache')
    expect(await screen.findByText(/Redis is not reachable/)).toBeInTheDocument()
  })
})
