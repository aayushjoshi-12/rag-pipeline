import '@testing-library/jest-dom/vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { doc, renderAt, resetStore, signInAs } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: { health: vi.fn(), listDocuments: vi.fn(), chat: vi.fn(), chatStream: vi.fn() },
  ApiError: class ApiError extends Error {},
}))
const m = vi.mocked(api)

const D1 = 'd1000000-0000-0000-0000-000000000001'
const D2 = 'd2000000-0000-0000-0000-000000000002'
const sources = [
  {
    text: 'The minimum credit score is 620 for conventional loans.',
    document_id: D1,
    chunk_index: 4,
    filename: 'guide.md',
    score: 0.2,
    rank: 1,
  },
  {
    text: 'Reserves are required after closing.',
    document_id: D2,
    chunk_index: 9,
    filename: 'other.md',
    score: 0.3,
    rank: 2,
  },
  {
    text: 'Neighbouring text about scores.',
    document_id: D1,
    chunk_index: 5,
    filename: 'guide.md',
    score: null,
    is_hit: false,
    context_for: { document_id: D1, chunk_index: 4 },
  },
]

const askIt = async (answer: string) => {
  m.chat.mockResolvedValue({ answer, sources } as never)
  renderAt('/chat')
  await userEvent.type(screen.getByLabelText('Question'), 'minimum credit score')
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
  return screen.findByLabelText('answer')
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
  Element.prototype.scrollIntoView = vi.fn()
})

describe('inline citations', () => {
  it('renders markers as buttons with the source in their name; unknown markers stay text', async () => {
    const section = await askIt('Score is 620 [1][2], see [3] and [9] and [0].')
    const marks = within(section)
      .getAllByRole('button', { name: /^Source \d/ })
      .filter((b) => b.className === 'cite')
    expect(marks.map((b) => b.getAttribute('aria-label'))).toEqual([
      'Source 1: guide.md, chunk 4',
      'Source 2: other.md, chunk 9',
      'Source 3: guide.md, chunk 5',
    ])
    expect(section.querySelector('.answer')).toHaveTextContent('and [9] and [0].')
  })

  it('shows a tooltip preview on hover and focus, Escape closes it', async () => {
    const section = await askIt('Yes [1].')
    const cite = within(section).getByRole('button', { name: /^Source 1: guide.md/ })
    expect(screen.queryByRole('tooltip')).toBeNull()
    await userEvent.hover(cite)
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('guide.md, chunk 4')
    expect(tip).toHaveTextContent(/minimum credit score is 620/)
    expect(cite).toHaveAttribute('aria-describedby', tip.id)
    expect(tip.querySelectorAll('mark').length).toBeGreaterThan(0) // matched query terms
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(cite).not.toHaveAttribute('aria-describedby')
    await userEvent.unhover(cite)
    await userEvent.tab() // keyboard: focus opens it too
    act(() => cite.focus())
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    act(() => cite.blur())
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('click or Enter scrolls to the source row, focuses and highlights it', async () => {
    const section = await askIt('Yes [2].')
    const cite = within(section).getByRole('button', { name: /^Source 2: other.md/ })
    cite.focus()
    await userEvent.keyboard('{Enter}')
    const row = document.getElementById('src-2')!
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
    expect(row).toHaveFocus()
    expect(row).toHaveClass('is-flash')
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('grouped sources', () => {
  it('shows each filename once with its passage count and a one-line snippet per passage', async () => {
    const section = await askIt('x')
    const groups = within(section).getAllByRole('region')
    expect(groups.map((g) => g.getAttribute('aria-label'))).toEqual(['guide.md', 'other.md'])
    expect(within(groups[0]!).getByRole('heading', { level: 3 })).toHaveTextContent(
      '1 passage + 1 context',
    )
    expect(within(groups[0]!).getAllByRole('listitem')).toHaveLength(2)
    expect(within(section).getAllByText('guide.md')).toHaveLength(1)
    expect(
      within(groups[0]!).getAllByRole('listitem')[0]!.querySelectorAll('mark').length,
    ).toBeGreaterThan(0)
  })

  it('expands to the full chunk with neighbour links, Open document and Copy citation', async () => {
    const section = await askIt('x')
    const first = within(section).getByRole('button', { name: /^Source 1:/ })
    await userEvent.click(first)
    const row = document.getElementById('src-1')!
    expect(within(row).getByRole('link', { name: 'Open document' })).toHaveAttribute(
      'href',
      `/documents/${D1}`,
    )
    await userEvent.click(within(row).getByRole('button', { name: 'source 3' })) // neighbour link
    expect(document.getElementById('src-3')).toHaveFocus()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await userEvent.click(within(row).getByRole('button', { name: /Copy citation/ }))
    expect(writeText).toHaveBeenCalledWith('guide.md, chunk 4')
    expect(await within(row).findByText('Copied')).toBeInTheDocument()
    await waitFor(() => expect(within(row).queryByText('Copied')).toBeNull(), { timeout: 3000 })
  })

  it('reports a clipboard failure and hides Open document without documents:read', async () => {
    signInAs(['chat:use'])
    const section = await askIt('x')
    await userEvent.click(within(section).getByRole('button', { name: /^Source 1:/ }))
    const row = document.getElementById('src-1')!
    expect(within(row).queryByRole('link', { name: 'Open document' })).toBeNull()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('no')) },
      configurable: true,
    })
    fireEvent.click(within(row).getByRole('button', { name: /Copy citation/ }))
    expect(await within(row).findByText('Could not copy')).toBeInTheDocument()
  })

  it('puts raw scores and ranks inside a details disclosure and exposes a non-numeric relevance meter', async () => {
    const section = await askIt('x')
    await userEvent.click(within(section).getByRole('button', { name: /^Source 1:/ }))
    const row = document.getElementById('src-1')!
    expect(row.querySelector('details')).toHaveTextContent('distance 0.20')
    expect(
      within(row).getByRole('img', { name: 'Relevance: high (ranked 1 of 2)' }),
    ).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /^Source 1:/ })).toHaveTextContent(/^1/)
  })

  it('uses the doc fixture shape', () => {
    expect(doc().status).toBe('ready')
  })
})
