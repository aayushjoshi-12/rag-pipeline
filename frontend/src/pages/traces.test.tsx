import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderAt, resetStore, signInAs } from '../test/helpers'
import type { DayUsage, Insights, Trace } from '../types'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    listTraces: vi.fn(),
    usage: vi.fn(),
    usageInsights: vi.fn(),
    usageCredits: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

const m = vi.mocked(api)

const trace = (over: Partial<Trace> = {}): Trace => ({
  id: 't1',
  created_at: '2026-11-14T10:00:00',
  kind: 'chat',
  status: 'ok',
  duration_ms: 1234,
  user_email: 'bob@example.test',
  document_id: null,
  model: 'some/model',
  prompt_tokens: 120,
  completion_tokens: 9,
  hits: 3,
  cost_usd: null,
  error: null,
  question: null,
  meta: { top_k: 5 },
  ...over,
})
const day = (over: Partial<DayUsage> = {}): DayUsage => ({
  day: '2026-11-14',
  requests: 10,
  errors: 2,
  avg_ms: 500,
  p95_ms: 900,
  prompt_tokens: 1000,
  completion_tokens: 200,
  cost_usd: null,
  unpriced_requests: 0,
  ...over,
})

const emptyInsights = (over: Partial<Insights> = {}): Insights => ({
  days: 14,
  kind: null,
  cost_usd: null,
  priced_chats: 0,
  unpriced_chats: 0,
  avg_cost_per_chat: null,
  by_model: [],
  latency_bucket_ms: 1,
  latency_histogram: [],
  errors_by_reason: [],
  heatmap: [],
  rag_days: [],
  hits_histogram: [],
  top_documents: [],
  cited_documents: 0,
  total_documents: 0,
  never_cited: 0,
  reuse_days: [],
  ingest_modes: [],
  ingest_failures: [],
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  resetStore()
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  m.listDocuments.mockResolvedValue([])
  m.listTraces.mockResolvedValue({ items: [], total: 0 })
  m.usage.mockResolvedValue([])
  m.usageInsights.mockResolvedValue(emptyInsights())
  m.usageCredits.mockResolvedValue({
    available: false,
    reason: 'no_api_key',
    limit: null,
    limit_remaining: null,
    usage: null,
    is_free_tier: null,
    fetched_at: null,
  })
})

describe('permission gating', () => {
  it.each(['/traces', '/usage'])(
    'hides the links and explains on a deep link to %s without traces:read',
    async (path) => {
      signInAs(['documents:read', 'chat:use', 'users:manage'])
      renderAt(path)
      expect(await screen.findByRole('alert')).toHaveTextContent('traces:read')
      expect(screen.queryByRole('link', { name: 'Traces' })).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'Usage' })).not.toBeInTheDocument()
      expect(m.listTraces).not.toHaveBeenCalled()
      expect(m.usage).not.toHaveBeenCalled()
      expect(m.usageInsights).not.toHaveBeenCalled()
      expect(m.usageCredits).not.toHaveBeenCalled()
    },
  )
  it('shows the links with the permission', async () => {
    renderAt('/')
    expect(await screen.findByRole('link', { name: 'Traces' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Usage' })).toBeInTheDocument()
  })
})

describe('Traces page', () => {
  it('shows the empty state', async () => {
    renderAt('/traces')
    expect(await screen.findByText(/No traces yet/)).toBeInTheDocument()
  })

  it('lists traces with status marks (icon + word, not colour alone) and tokens', async () => {
    m.listTraces.mockResolvedValue({
      items: [
        trace(),
        trace({
          id: 't2',
          status: 'error',
          error: 'LLM provider returned 429',
          prompt_tokens: null,
          completion_tokens: null,
          hits: null,
        }),
      ],
      total: 2,
    })
    renderAt('/traces')
    expect(await screen.findByText('✓ ok')).toBeInTheDocument()
    expect(screen.getByText('✕ error')).toBeInTheDocument()
    expect(screen.getByText('120 / 9')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2) // missing tokens + hits
    expect(screen.getAllByText('1,234 ms')).toHaveLength(2)
  })

  it('distinguishes cached, n/a and real numbers in the table, with explanatory titles', async () => {
    m.listTraces.mockResolvedValue({
      items: [
        trace({
          id: 'a',
          prompt_tokens: 0,
          completion_tokens: 0,
          cost_usd: 0,
          meta: { cache: { answer: 'hit' } },
        }),
        trace({ id: 'b', prompt_tokens: null, completion_tokens: null, cost_usd: null }),
        trace({
          id: 'c',
          prompt_tokens: 2500,
          completion_tokens: 2048,
          cost_usd: 0.00011,
          status: 'error',
          error: 'x',
        }),
      ],
      total: 3,
    })
    renderAt('/traces')
    expect(await screen.findByText('cached')).toHaveAttribute(
      'title',
      expect.stringMatching(/cache/),
    )
    const na = screen.getAllByText('n/a')
    expect(na).toHaveLength(2) // tokens and cost
    expect(na[0]).toHaveAttribute('title', expect.stringMatching(/provider returned no count/))
    expect(screen.getByText('2,500 / 2,048')).toBeInTheDocument()
    expect(screen.getByText('$0.00011')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Cost' })).toBeInTheDocument()
  })

  it('expands a row to show the error and details', async () => {
    m.listTraces.mockResolvedValue({
      items: [
        trace({ id: 't2', status: 'error', error: 'LLM provider returned 429', question: 'why?' }),
      ],
      total: 1,
    })
    renderAt('/traces')
    await userEvent.click(await screen.findByRole('button', { name: 'details t2' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('LLM provider returned 429')
    expect(screen.getByText('why?')).toBeInTheDocument()
    // known settings are chips; the raw JSON sits behind the Raw toggle
    expect(screen.queryByText('{"top_k":5}')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Raw' }))
    expect(screen.getByText('{"top_k":5}')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'details t2' }))
    expect(screen.queryByText('why?')).not.toBeInTheDocument()
  })

  it('shows retrieval settings as chips with the raw JSON behind a toggle', async () => {
    const meta = {
      mode: 'hybrid',
      keyword_method: 'bm25',
      fusion: 'rrf',
      candidates: 50,
      top_k: 5,
      neighbors: 1,
      extra: { a: 1 },
    }
    m.listTraces.mockResolvedValue({ items: [trace({ id: 't9', meta })], total: 1 })
    renderAt('/traces')
    await userEvent.click(await screen.findByRole('button', { name: 'details t9' }))
    const chips = within(screen.getByRole('list', { name: 'retrieval settings' }))
    for (const [k, v] of [
      ['mode', 'hybrid'],
      ['fusion', 'rrf'],
      ['keyword method', 'bm25'],
      ['candidates', '50'],
      ['top_k', '5'],
      ['neighbors', '1'],
    ] as const) {
      const item = chips.getByText(k).closest('li')!
      expect(item).toHaveTextContent(`${k} ${v}`)
    }
    expect(chips.getAllByRole('listitem')).toHaveLength(6) // objects are not chips
    expect(screen.queryByText(JSON.stringify(meta))).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Raw' }))
    expect(screen.getByText(JSON.stringify(meta))).toBeInTheDocument()
  })

  it('omits chips for settings that are absent (semantic trace without fusion)', async () => {
    m.listTraces.mockResolvedValue({
      items: [
        trace({
          id: 't8',
          meta: { mode: 'semantic', fusion: null, top_k: 5, candidates: { n: 1 } },
        }),
      ],
      total: 1,
    })
    renderAt('/traces')
    await userEvent.click(await screen.findByRole('button', { name: 'details t8' }))
    const chips = within(screen.getByRole('list', { name: 'retrieval settings' }))
    expect(chips.getAllByRole('listitem')).toHaveLength(2)
    expect(chips.queryByText('fusion')).not.toBeInTheDocument()
  })

  it('filters by type and status and resets to the first page', async () => {
    m.listTraces.mockResolvedValue({ items: [trace()], total: 60 })
    renderAt('/traces')
    await screen.findByText('✓ ok')
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() =>
      expect(m.listTraces).toHaveBeenLastCalledWith({
        kind: '',
        status: '',
        limit: 25,
        offset: 25,
      }),
    )
    await userEvent.selectOptions(screen.getByLabelText('Type'), 'search')
    await waitFor(() =>
      expect(m.listTraces).toHaveBeenLastCalledWith({
        kind: 'search',
        status: '',
        limit: 25,
        offset: 0,
      }),
    )
    await userEvent.selectOptions(screen.getByLabelText('Type'), 'ingest')
    await waitFor(() =>
      expect(m.listTraces).toHaveBeenLastCalledWith({
        kind: 'ingest',
        status: '',
        limit: 25,
        offset: 0,
      }),
    )
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'error')
    await waitFor(() =>
      expect(m.listTraces).toHaveBeenLastCalledWith({
        kind: 'ingest',
        status: 'error',
        limit: 25,
        offset: 0,
      }),
    )
  })

  it('paginates: Previous disabled on page one, Next disabled on the last page', async () => {
    m.listTraces.mockResolvedValue({ items: [trace()], total: 30 })
    renderAt('/traces')
    await screen.findByText('✓ ok')
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    expect(screen.getByText(/1–25 of 30/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled())
    expect(screen.getByText(/26–30 of 30/)).toBeInTheDocument()
  })

  it('surfaces a load error', async () => {
    m.listTraces.mockRejectedValue(new Error('boom'))
    renderAt('/traces')
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })
})

describe('Usage page', () => {
  it('computes the headline tiles from the daily data', async () => {
    m.usage.mockResolvedValue([
      day({
        day: '2026-11-13',
        requests: 10,
        errors: 1,
        avg_ms: 100,
        prompt_tokens: 1000,
        completion_tokens: 500,
      }),
      day({
        day: '2026-11-14',
        requests: 30,
        errors: 5,
        avg_ms: 200,
        prompt_tokens: 2000,
        completion_tokens: 500,
      }),
    ])
    renderAt('/usage')
    expect(await screen.findByTestId('tile-requests')).toHaveTextContent('40')
    expect(screen.getByTestId('tile-error-rate')).toHaveTextContent('15.0%') // 6 / 40
    expect(screen.getByTestId('tile-latency')).toHaveTextContent('175 ms') // (10*100 + 30*200) / 40
    expect(screen.getByTestId('tile-tokens')).toHaveTextContent('4k') // 1000+500+2000+500
  })

  it('shows dashes instead of NaN when there is no traffic', async () => {
    m.usage.mockResolvedValue([
      day({
        requests: 0,
        errors: 0,
        avg_ms: null,
        p95_ms: null,
        prompt_tokens: 0,
        completion_tokens: 0,
      }),
    ])
    renderAt('/usage')
    expect(await screen.findByTestId('tile-error-rate')).toHaveTextContent('—')
    expect(screen.getByTestId('tile-latency')).toHaveTextContent('—')
    expect(screen.getByTestId('tile-requests')).toHaveTextContent('0')
  })

  it('renders the cost tab charts with an accessible table view (default tab)', async () => {
    m.usage.mockResolvedValue([day({ cost_usd: 0.0125, unpriced_requests: 1 })])
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    expect(screen.getByRole('tab', { name: 'Cost', selected: true })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Spend per day' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Cumulative spend' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'LLM tokens per day' })).toBeInTheDocument()
    expect(screen.getAllByLabelText('legend')).toHaveLength(1) // only the two-series token chart
    const table = screen.getByRole('table', { name: 'usage by day', hidden: true })
    expect(within(table).getByText('2026-11-14')).toBeInTheDocument()
    expect(within(table).getByText('500 ms')).toBeInTheDocument()
    expect(within(table).getByText('900 ms')).toBeInTheDocument()
    expect(within(table).getByText('$0.0125')).toBeInTheDocument()
  })

  it('renders the requests-per-day chart with a legend on the performance tab', async () => {
    m.usage.mockResolvedValue([day()])
    renderAt('/usage?tab=performance')
    await screen.findByTestId('tile-requests')
    expect(screen.getByRole('img', { name: 'Requests per day' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Latency per day' })).toBeInTheDocument()
    expect(screen.getAllByLabelText('legend')).toHaveLength(2)
  })

  it('re-queries when the period or type changes', async () => {
    m.usage.mockResolvedValue([day()])
    renderAt('/usage')
    await screen.findByTestId('tile-requests')
    expect(m.usage).toHaveBeenLastCalledWith(14, '')
    await userEvent.selectOptions(screen.getByLabelText('Period'), '30')
    await waitFor(() => expect(m.usage).toHaveBeenLastCalledWith(30, ''))
    await userEvent.selectOptions(screen.getByLabelText('Type'), 'chat')
    await waitFor(() => expect(m.usage).toHaveBeenLastCalledWith(30, 'chat'))
  })

  it('surfaces a load error', async () => {
    m.usage.mockRejectedValue(new Error('usage unavailable'))
    renderAt('/usage')
    expect(await screen.findByRole('alert')).toHaveTextContent('usage unavailable')
  })
})
