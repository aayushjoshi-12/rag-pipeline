import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearTokens } from '../auth/session'
import { ApiError, api } from './client'
import { StreamUnavailableError, createSseParser, readChatStream, toChatEvent } from './stream'
import type { ChatStreamEvent } from '../types'

const frames = (text: string, pieces: number): [string, string][] => {
  const out: [string, string][] = []
  const feed = createSseParser((e, d) => out.push([e, d]))
  const size = Math.ceil(text.length / pieces)
  for (let i = 0; i < text.length; i += size) feed(text.slice(i, i + size))
  return out
}

const SAMPLE =
  'event: sources\ndata: {"sources":[],"cache":"miss"}\n\n' +
  'event: token\ndata: {"text":"Hel"}\n\n' +
  'event: token\ndata: {"text":"lo"}\n\n'

describe('SSE parser', () => {
  it('parses whole frames', () => {
    expect(frames(SAMPLE, 1).map(([e]) => e)).toEqual(['sources', 'token', 'token'])
  })

  it.each([2, 7, 50, SAMPLE.length])('is independent of how the bytes are chunked (%i)', (n) => {
    expect(frames(SAMPLE, n)).toEqual(frames(SAMPLE, 1))
  })

  it('handles CRLF, comments, multi-line data and an unterminated tail', () => {
    const out = frames(
      ': keep-alive\r\n\r\nevent: x\r\ndata: a\r\ndata: b\r\n\r\nevent: y\ndata: tail',
      3,
    )
    expect(out).toEqual([['x', 'a\nb']]) // the tail has no blank line yet: not emitted
  })

  it('does not strip more than one leading space of data', () => {
    expect(frames('data:  two\n\n', 1)).toEqual([['message', ' two']])
  })
})

describe('toChatEvent', () => {
  it('types the four events and ignores unknown or malformed frames', () => {
    expect(toChatEvent('token', '{"text":"a"}')).toEqual({ event: 'token', data: { text: 'a' } })
    expect(toChatEvent('token', '{"nope":1}')).toBeNull()
    expect(toChatEvent('token', 'not json')).toBeNull()
    expect(toChatEvent('future-event', '{}')).toBeNull()
    expect(toChatEvent('error', '{"detail":"x"}')).toEqual({
      event: 'error',
      data: { status: 500, detail: 'x', trace_id: null },
    })
  })
})

function bodyOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch))
      c.close()
    },
  })
}

describe('readChatStream', () => {
  it('delivers events in order, even with a multi-byte character split across chunks', async () => {
    const bytes = new TextEncoder().encode('event: token\ndata: {"text":"é"}\n\n')
    const cut = 27 // inside the two-byte é
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(bytes.slice(0, cut))
        c.enqueue(bytes.slice(cut))
        c.close()
      },
    })
    const got: ChatStreamEvent[] = []
    await readChatStream(body, (e) => got.push(e))
    expect(got).toEqual([{ event: 'token', data: { text: 'é' } }])
  })
})

describe('api.chatStream', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearTokens()
  })

  const stub = (res: Response | Error | DOMException) =>
    vi.stubGlobal(
      'fetch',
      res instanceof Response ? vi.fn().mockResolvedValue(res) : vi.fn().mockRejectedValue(res),
    )

  it('streams events from an event-stream response and posts to /chat/stream', async () => {
    stub(new Response(bodyOf([SAMPLE]), { headers: { 'Content-Type': 'text/event-stream' } }))
    const got: string[] = []
    await api.chatStream({ question: 'q' }, (e) => got.push(e.event))
    expect(got).toEqual(['sources', 'token', 'token'])
    const call = vi.mocked(fetch).mock.calls[0]
    expect(call?.[0]).toBe('/api/chat/stream')
  })

  it.each([404, 405, 501])('HTTP %i means "no streaming here": fall back', async (status) => {
    stub(new Response('{}', { status }))
    await expect(api.chatStream({ question: 'q' }, () => {})).rejects.toBeInstanceOf(
      StreamUnavailableError,
    )
  })

  it('a network failure opening the stream means fall back', async () => {
    stub(new TypeError('failed to fetch'))
    await expect(api.chatStream({ question: 'q' }, () => {})).rejects.toBeInstanceOf(
      StreamUnavailableError,
    )
  })

  it('a 200 that is not an event stream (a buffering proxy) means fall back', async () => {
    stub(new Response('<html>', { headers: { 'Content-Type': 'text/html' } }))
    await expect(api.chatStream({ question: 'q' }, () => {})).rejects.toBeInstanceOf(
      StreamUnavailableError,
    )
  })

  it.each([401, 403, 422, 503])('HTTP %i is a real error, not a fallback', async (status) => {
    stub(
      new Response(JSON.stringify({ detail: 'nope' }), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const err = await api.chatStream({ question: 'q' }, () => {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(status)
    expect((err as ApiError).message).toBe('nope')
  })

  it('passes an abort through as AbortError (not a fallback)', async () => {
    stub(new DOMException('aborted', 'AbortError'))
    const err = await api.chatStream({ question: 'q' }, () => {}).catch((e: unknown) => e)
    expect(err).not.toBeInstanceOf(ApiError)
    expect((err as DOMException).name).toBe('AbortError')
  })
})
