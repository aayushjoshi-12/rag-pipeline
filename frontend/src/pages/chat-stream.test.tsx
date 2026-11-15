import '@testing-library/jest-dom/vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { StreamUnavailableError } from '../api/stream'
import { renderAt, resetStore } from '../test/helpers'
import type { ChatStreamEvent } from '../types'

vi.mock('../config', () => ({ POLL_MS: 10 }))
vi.mock('../api/client', () => ({
  api: { health: vi.fn(), listDocuments: vi.fn(), chat: vi.fn(), chatStream: vi.fn() },
  ApiError: class ApiError extends Error {},
}))
const m = vi.mocked(api)

const source = {
  text: 'The battery lasts 14 hours.',
  document_id: 'd1000000-0000-0000-0000-000000000001',
  chunk_index: 3,
  filename: 'zephyr.md',
  score: 0.1,
}
const sources: ChatStreamEvent = { event: 'sources', data: { sources: [source], cache: 'miss' } }
const token = (text: string): ChatStreamEvent => ({ event: 'token', data: { text } })
const done = (answer: string, cache = 'miss'): ChatStreamEvent => ({
  event: 'done',
  data: {
    answer,
    usage: { prompt_tokens: 10, completion_tokens: 3 },
    cost_usd: 0,
    cache,
    trace_id: 't1',
  },
})

/** A chatStream whose events the test emits by hand, one at a time. */
function controlled() {
  let emit: (e: ChatStreamEvent) => void = () => {}
  let finish: () => void = () => {}
  let fail: (e: unknown) => void = () => {}
  let signal: AbortSignal | undefined
  m.chatStream.mockImplementation((_req, onEvent, sig) => {
    emit = onEvent
    signal = sig
    sig?.addEventListener('abort', () => fail(new DOMException('aborted', 'AbortError')))
    return new Promise<void>((resolve, reject) => {
      finish = resolve
      fail = reject
    })
  })
  return {
    emit: (e: ChatStreamEvent) => act(() => emit(e)),
    finish: () => act(() => finish()),
    fail: (e: unknown) => act(() => fail(e)),
    get signal() {
      return signal
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  resetStore()
  m.listDocuments.mockResolvedValue([])
  m.health.mockResolvedValue({ status: 'ok', checks: {} })
})

const ask = async (q = 'How long is the battery?') => {
  await userEvent.type(screen.getByLabelText('Question'), q)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
}

describe('streaming chat', () => {
  it('shows sources as soon as they arrive, then grows the answer token by token', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    expect(m.chatStream).toHaveBeenCalledWith(
      { question: 'How long is the battery?' },
      expect.any(Function),
      expect.any(AbortSignal),
    )
    expect(screen.getByText(/Searching documents/)).toBeInTheDocument()

    await s.emit(sources)
    const section = await screen.findByLabelText('answer')
    expect(within(section).getByText(/zephyr\.md · chunk 3/)).toBeInTheDocument() // sources first
    expect(section).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByText(/Searching documents/)).toBeNull()

    await s.emit(token('About '))
    expect(within(section).getByText(/About/)).toBeInTheDocument()
    await s.emit(token('14 hours [1].'))
    expect(section.querySelector('.answer')).toHaveTextContent('About 14 hours [1].')
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()

    await s.emit(done('About 14 hours [1].'))
    await s.finish()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ask' })).toBeInTheDocument())
    expect(section).not.toHaveAttribute('aria-busy')
    expect(m.chat).not.toHaveBeenCalled() // streaming worked: no second request
  })

  it('Stop aborts the request and keeps the partial answer', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    await s.emit(sources)
    await s.emit(token('Partial'))
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(s.signal?.aborted).toBe(true)
    expect(await screen.findByText(/The answer above is partial/)).toBeInTheDocument()
    expect(screen.getByLabelText('answer').querySelector('.answer')).toHaveTextContent('Partial')
    expect(screen.queryByRole('alert')).toBeNull() // a deliberate stop is not an error
    expect(screen.getByRole('button', { name: 'Ask' })).toBeInTheDocument()
  })

  it('Stop before any source arrives just ends the request', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(s.signal?.aborted).toBe(true)
    expect(await screen.findByText('Stopped.')).toBeInTheDocument()
    expect(screen.queryByLabelText('answer')).toBeNull()
  })

  it('an in-stream error keeps the partial answer and shows the message', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    await s.emit(sources)
    await s.emit(token('Half an ans'))
    await s.emit({
      event: 'error',
      data: { status: 503, detail: 'LLM provider returned 503', trace_id: null },
    })
    await s.finish()
    expect(await screen.findByRole('alert')).toHaveTextContent('LLM provider returned 503')
    expect(screen.getByLabelText('answer').querySelector('.answer')).toHaveTextContent(
      'Half an ans',
    )
    expect(screen.getByRole('button', { name: 'Ask' })).toBeInTheDocument()
  })

  it('falls back to the plain endpoint when the stream cannot be opened', async () => {
    m.chatStream.mockRejectedValue(new StreamUnavailableError('HTTP 404'))
    m.chat.mockResolvedValue({ answer: 'Plain answer [1]', sources: [source] })
    renderAt('/chat')
    await ask()
    const section = await screen.findByLabelText('answer')
    expect(within(section).getByText('Plain answer [1]')).toBeInTheDocument()
    expect(m.chat).toHaveBeenCalledWith({ question: 'How long is the battery?' })
  })

  it('falls back when the stream ends without any event', async () => {
    m.chatStream.mockResolvedValue(undefined)
    m.chat.mockResolvedValue({ answer: 'Plain', sources: [] })
    renderAt('/chat')
    await ask()
    expect(await screen.findByText('Plain')).toBeInTheDocument()
  })

  it('a real HTTP error is shown and does NOT trigger the fallback', async () => {
    m.chatStream.mockRejectedValue(new Error('missing permission: chat:use'))
    renderAt('/chat')
    await ask()
    expect(await screen.findByRole('alert')).toHaveTextContent('missing permission: chat:use')
    expect(m.chat).not.toHaveBeenCalled()
  })

  it('a stream cut off after some tokens is an error, not a silent second request', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    await s.emit(sources)
    await s.emit(token('abc'))
    await s.finish() // closed with no done/error
    expect(await screen.findByRole('alert')).toHaveTextContent(/ended unexpectedly/)
    expect(m.chat).not.toHaveBeenCalled()
  })

  it('shows a cache hit note after done', async () => {
    const s = controlled()
    renderAt('/chat')
    await ask()
    await s.emit(sources)
    await s.emit(token('x'))
    await s.emit(done('x', 'hit'))
    await s.finish()
    expect(await screen.findByText(/Served from the answer cache/)).toBeInTheDocument()
  })
})
