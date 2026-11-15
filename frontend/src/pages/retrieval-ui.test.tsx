import '@testing-library/jest-dom/vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { STORAGE_KEY } from '../retrieval'
import { renderAt, resetStore, signInAs } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: { health: vi.fn(), listDocuments: vi.fn(), chat: vi.fn(), chatStream: vi.fn() },
  ApiError: class ApiError extends Error {},
}))
const m = vi.mocked(api)

const src = (over: Record<string, unknown> = {}) => ({
  text: 'passage',
  document_id: 'd1000000-0000-0000-0000-000000000001',
  chunk_index: 1,
  filename: 'a.md',
  score: 0.5,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  m.chat.mockResolvedValue({ answer: 'ok', sources: [] })
})

const ask = async (q = 'q') => {
  await userEvent.type(screen.getByLabelText('Question'), q)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
}
const openOptions = () => userEvent.click(screen.getByRole('button', { name: /Advanced/ }))

describe('retrieval options in the composer', () => {
  it('sends neither mode nor fusion when left on Server default', async () => {
    renderAt('/chat')
    await ask()
    await waitFor(() => expect(m.chat).toHaveBeenCalledWith({ question: 'q' }))
  })

  it('sends only the overridden mode', async () => {
    renderAt('/chat')
    await openOptions()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'keyword')
    await ask()
    await waitFor(() => expect(m.chat).toHaveBeenCalledWith({ question: 'q', mode: 'keyword' }))
  })

  it('shows Fusion only for hybrid and sends it', async () => {
    renderAt('/chat')
    await openOptions()
    expect(screen.queryByLabelText('Fusion')).toBeNull() // server default
    for (const mode of ['semantic', 'keyword']) {
      await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), mode)
      expect(screen.queryByLabelText('Fusion')).toBeNull()
    }
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'hybrid')
    await userEvent.selectOptions(screen.getByLabelText('Fusion'), 'weighted')
    await ask()
    await waitFor(() =>
      expect(m.chat).toHaveBeenCalledWith({ question: 'q', mode: 'hybrid', fusion: 'weighted' }),
    )
  })

  it('sends top_k and neighbors (0 included) and flags the active override', async () => {
    renderAt('/chat')
    expect(screen.queryByTestId('retrieval-summary')).toBeNull() // nothing overridden, no indicator
    await openOptions()
    await userEvent.type(screen.getByLabelText(/Passages/), '8')
    await userEvent.selectOptions(screen.getByLabelText('Neighbours'), '0')
    expect(screen.getByTestId('retrieval-summary')).toHaveTextContent('top 8 · ±0 neighbours')
    await ask()
    await waitFor(() =>
      expect(m.chat).toHaveBeenCalledWith({ question: 'q', top_k: 8, neighbors: 0 }),
    )
  })

  it('Reset clears overrides; Esc closes the popover and returns focus', async () => {
    renderAt('/chat')
    await openOptions()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'keyword')
    expect(screen.getByTestId('retrieval-summary')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }))
    expect(screen.queryByTestId('retrieval-summary')).toBeNull()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: /Advanced/ })).toHaveFocus()
  })

  it('links to Admin > Settings only for users who can read settings', async () => {
    signInAs(['chat:use', 'documents:read'])
    renderAt('/chat')
    await openOptions()
    expect(screen.queryByRole('link', { name: /Admin > Settings/ })).toBeNull()
    expect(screen.getByText(/Defaults are set in/)).toHaveTextContent('Admin > Settings')
  })

  it('links to Admin > Settings for users who can read settings', async () => {
    signInAs(['chat:use', 'settings:read'])
    renderAt('/chat')
    await openOptions()
    expect(screen.getByRole('link', { name: /Admin > Settings/ })).toHaveAttribute(
      'href',
      '/settings',
    )
  })

  it('explains the selected mode in one line', async () => {
    renderAt('/chat')
    await openOptions()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'keyword')
    expect(screen.getByText(/exact words, section numbers and IDs/)).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'hybrid')
    expect(screen.getByText(/Combines meaning and exact words/)).toBeInTheDocument()
  })

  it('remembers the choice in localStorage and restores it', async () => {
    const first = renderAt('/chat')
    await openOptions()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'hybrid')
    await userEvent.selectOptions(screen.getByLabelText('Fusion'), 'rrf')
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      mode: 'hybrid',
      fusion: 'rrf',
      topK: '',
      neighbors: '',
    })
    first.unmount()
    renderAt('/chat')
    expect(screen.getByTestId('retrieval-summary')).toHaveTextContent('Hybrid · RRF')
    await ask()
    await waitFor(() =>
      expect(m.chat).toHaveBeenCalledWith({ question: 'q', mode: 'hybrid', fusion: 'rrf' }),
    )
  })

  it('still works when localStorage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    renderAt('/chat')
    await openOptions()
    await userEvent.selectOptions(screen.getByLabelText('Retrieval mode'), 'keyword')
    await ask()
    await waitFor(() => expect(m.chat).toHaveBeenCalledWith({ question: 'q', mode: 'keyword' }))
    vi.restoreAllMocks()
  })

  it('keeps the existing accessible names', () => {
    renderAt('/chat')
    expect(screen.getByLabelText('Question')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Search in/ })).toBeInTheDocument()
  })

  it.each([
    ['rrf-keyword-503', 'keyword index is empty: run rebuild-fts first'],
    ['422', 'mode: Input should be semantic, keyword or hybrid'],
  ])('shows the server message in the alert (%s)', async (_n, message) => {
    m.chat.mockRejectedValue(new Error(message))
    renderAt('/chat')
    await ask()
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
  })
})

describe('source rows', () => {
  const answerWith = async (sources: Record<string, unknown>[]) => {
    m.chat.mockResolvedValue({ answer: 'ok', sources: sources.map((s) => src(s)) as never })
    renderAt('/chat')
    await ask()
    const section = await screen.findByLabelText('answer')
    for (const b of within(section).getAllByRole('button', { name: /^Source \d+:/ })) {
      await userEvent.click(b)
    }
    return within(section).getAllByRole('listitem')
  }

  it('labels each score by its kind, inside the details disclosure', async () => {
    const items = await answerWith([
      { score: 0.42, score_kind: 'distance' },
      { score: 11.84, score_kind: 'bm25' },
      { score: 0.031, score_kind: 'rrf' },
      { score: 0.5 },
      { score: 0.7, score_kind: 'mystery' },
    ])
    expect(within(items[0]!).getByText('distance 0.42').closest('details')).not.toBeNull()
    expect(within(items[1]!).getByText('BM25 11.8')).toBeInTheDocument()
    expect(within(items[1]!).getByTitle(/higher is better/)).toBeInTheDocument()
    expect(within(items[2]!).getByText('RRF 0.031')).toBeInTheDocument()
    expect(within(items[3]!).getByText('distance 0.50')).toBeInTheDocument()
    expect(within(items[4]!).getByText('score 0.70')).toBeInTheDocument()
    expect(within(items[0]!).getByTitle(/lower is closer/)).toBeInTheDocument()
  })

  it('shows rank rows for hybrid results only where a rank exists', async () => {
    const items = await answerWith([
      { score: 0.031, score_kind: 'rrf', semantic_rank: 2, keyword_rank: 1 },
      { score: 0.016, score_kind: 'rrf', semantic_rank: null, keyword_rank: 7 },
      { score: 0.4, score_kind: 'distance' },
      { score: 0.02, score_kind: 'rrf', semantic_rank: 3, keyword_rank: null },
    ])
    expect(within(items[0]!).getByText('semantic #2')).toBeInTheDocument()
    expect(within(items[0]!).getByText('keyword #1')).toBeInTheDocument()
    expect(within(items[1]!).queryByText(/semantic #/)).not.toBeInTheDocument()
    expect(within(items[1]!).getByText('keyword #7')).toBeInTheDocument()
    expect(within(items[2]!).queryByText(/#\d/)).not.toBeInTheDocument()
    expect(within(items[3]!).getByText('semantic #3')).toBeInTheDocument()
    expect(within(items[3]!).queryByText(/keyword #/)).not.toBeInTheDocument()
  })

  it('keeps the context tag for neighbours', async () => {
    const items = await answerWith([{ is_hit: false, score_kind: 'bm25', score: null }])
    expect(within(items[0]!).getByText('context')).toBeInTheDocument()
  })

  it('shows no score, rank or distance on a context chunk, but does on its hit', async () => {
    const items = await answerWith([
      { is_hit: false, score: null, score_kind: 'rrf', rank: null },
      { is_hit: true, score: 0.45, score_kind: 'distance', rank: 1 },
    ])
    const ctx = within(items[0]!)
    expect(ctx.queryByText(/distance|RRF|BM25|weighted|score|not scored|NaN|null/)).toBeNull()
    expect(ctx.getByText('context')).toBeInTheDocument()
    expect(within(items[1]!).getByText('distance 0.45')).toBeInTheDocument()
  })
})
