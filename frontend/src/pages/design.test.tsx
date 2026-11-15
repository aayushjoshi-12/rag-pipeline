import { fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import { render } from '@testing-library/react'
import { signInAs, doc, renderAt, resetStore } from '../test/helpers'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: {
    health: vi.fn(),
    listDocuments: vi.fn(),
    getDocument: vi.fn(),
    listChunks: vi.fn(),
    deleteDocument: vi.fn(),
    getJob: vi.fn(),
    upload: vi.fn(),
    replace: vi.fn(),
    chat: vi.fn(),
    chatStream: vi.fn(),
  },
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
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
})

describe('chat conversation layout', () => {
  it('marks neighbour chunks as context and numbers every source', async () => {
    m.chat.mockResolvedValue({
      answer: 'ok',
      sources: [src({ filename: 'hit.md' }), src({ filename: 'near.md', is_hit: false })],
    })
    renderAt('/chat')
    await userEvent.type(screen.getByLabelText('Question'), 'q')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    const section = await screen.findByLabelText('answer')
    const items = within(section).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]!).queryByText('context')).not.toBeInTheDocument()
    expect(within(items[1]!).getByText('context')).toBeInTheDocument()
    expect(within(items[0]!).getByRole('img', { name: /^Relevance: high/ })).toBeInTheDocument()
    expect(within(items[1]!).queryByRole('img', { name: /Relevance/ })).toBeNull()
  })

  it('shows the question as a user message and says when nothing relevant was found', async () => {
    m.chat.mockResolvedValue({ answer: 'No idea.', sources: [] })
    renderAt('/chat')
    await userEvent.type(screen.getByLabelText('Question'), 'unknown topic')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByText(/No relevant documents were found/)).toBeVisible()
    expect(screen.getByText('unknown topic')).toBeInTheDocument()
  })

  it('passages start collapsed to one line and expand to the full chunk', async () => {
    const long = 'x'.repeat(400)
    m.chat.mockResolvedValue({ answer: 'ok', sources: [src({ text: long })] })
    renderAt('/chat')
    await userEvent.type(screen.getByLabelText('Question'), 'q')
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
    const toggle = await screen.findByRole('button', { name: /^Source 1:/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(long)).toBeNull() // only a windowed one-line snippet
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText(long)).toBeInTheDocument()
    await userEvent.click(toggle)
    expect(screen.queryByText(long)).toBeNull()
  })

  it('Enter sends, Shift+Enter does not', async () => {
    m.chat.mockResolvedValue({ answer: 'ok', sources: [] })
    renderAt('/chat')
    const box = screen.getByLabelText('Question')
    await userEvent.type(box, 'first{Shift>}{Enter}{/Shift}')
    expect(m.chat).not.toHaveBeenCalled()
    await userEvent.type(box, 'second{Enter}')
    expect(m.chat).toHaveBeenCalledTimes(1)
    expect(m.chat.mock.calls[0]![0].question).toContain('second')
  })
})

describe('upload dropzone', () => {
  it('lists dropped files and exposes them to the form', async () => {
    renderAt('/documents')
    const input = (await screen.findByLabelText('Document')) as HTMLInputElement
    const file = new File(['hello'], 'dropped.md', { type: 'text/markdown' })
    // jsdom rejects a plain array as `files`: make the property assignable for this test
    Object.defineProperty(input, 'files', { configurable: true, writable: true, value: null })
    fireEvent.drop(input.closest('label')!, { dataTransfer: { files: [file] } })
    expect(await screen.findByText('dropped.md')).toBeInTheDocument()
    expect(input.files?.[0]?.name).toBe('dropped.md')
  })
})

describe('app shell', () => {
  it('groups navigation by permission and keeps a skip link', async () => {
    signInAs(['documents:read'])
    renderAt('/')
    expect(await screen.findByRole('link', { name: 'Documents' })).toBeInTheDocument()
    expect(screen.getByText('Workspace')).toBeInTheDocument()
    expect(screen.queryByText('Observability')).not.toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main')
  })

  it('shows every group for an administrator and toggles the mobile menu', async () => {
    renderAt('/')
    expect(await screen.findByText('Observability')).toBeInTheDocument()
    expect(screen.getByText('Admin')).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: 'Open menu' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    await userEvent.click(screen.getByRole('link', { name: 'Overview' }))
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument()
  })
})

describe('status badge', () => {
  it('pairs an icon with the word and keeps the text exactly the status', () => {
    const { container } = render(<StatusBadge status="failed" />)
    const badge = screen.getByTestId('status-badge')
    expect(badge).toHaveTextContent(/^failed$/)
    expect(container.querySelector('svg[aria-hidden="true"]')).not.toBeNull()
    expect(badge).toHaveClass('badge-failed')
  })
})

describe('overview', () => {
  it('summarises the library and hides the chat action without chat:use', async () => {
    signInAs(['documents:read'])
    m.listDocuments.mockResolvedValue([doc(), doc({ id: 'x', status: 'failed', filename: 'b.md' })])
    renderAt('/')
    expect(await screen.findByText(/Total 2 · ready 1/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Ask a question' })).not.toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: '1 ready, 0 in progress, 1 failed' }),
    ).toBeInTheDocument()
  })
})
