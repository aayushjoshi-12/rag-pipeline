import type { ChatDone, ChatStreamEvent, Source } from '../types'

/** The stream could not be opened or was cut off before any event: the caller should fall back to `POST /chat`. */
export class StreamUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'StreamUnavailableError'
  }
}

/** Incremental Server-Sent Events parser: feed it decoded text in arbitrary pieces (frames may be split
 * anywhere, line endings may be LF or CRLF); `onFrame` fires once per complete frame. */
export function createSseParser(onFrame: (event: string, data: string) => void) {
  let buffer = ''
  return (chunk: string): void => {
    buffer += chunk
    for (;;) {
      const m = /\r?\n\r?\n/.exec(buffer)
      if (!m) return
      const frame = buffer.slice(0, m.index)
      buffer = buffer.slice(m.index + m[0].length)
      let event = 'message'
      const data: string[] = []
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith(':')) continue // comment / keep-alive
        const i = line.indexOf(':')
        const field = i === -1 ? line : line.slice(0, i)
        const value = i === -1 ? '' : line.slice(i + 1).replace(/^ /, '')
        if (field === 'event') event = value
        else if (field === 'data') data.push(value)
      }
      if (data.length > 0) onFrame(event, data.join('\n'))
    }
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

/** Turn one raw frame into a typed event; unknown or malformed frames are ignored (forward compatible). */
export function toChatEvent(event: string, raw: string): ChatStreamEvent | null {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }
  if (!isRecord(data)) return null
  switch (event) {
    case 'sources':
      return Array.isArray(data.sources)
        ? {
            event,
            data: { sources: data.sources as Source[], cache: String(data.cache ?? 'off') },
          }
        : null
    case 'token':
      return typeof data.text === 'string' ? { event, data: { text: data.text } } : null
    case 'done':
      return typeof data.answer === 'string' ? { event, data: data as unknown as ChatDone } : null
    case 'error':
      return {
        event,
        data: {
          status: typeof data.status === 'number' ? data.status : 500,
          detail: typeof data.detail === 'string' ? data.detail : 'The answer stream failed.',
          trace_id: typeof data.trace_id === 'string' ? data.trace_id : null,
        },
      }
    default:
      return null
  }
}

/** Read an SSE response body to the end, calling `onEvent` for every known event as it arrives. */
export async function readChatStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (e: ChatStreamEvent) => void,
): Promise<void> {
  const decoder = new TextDecoder()
  const parse = createSseParser((event, raw) => {
    const e = toChatEvent(event, raw)
    if (e) onEvent(e)
  })
  const reader = body.getReader()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parse(decoder.decode(value, { stream: true }))
    }
    parse(decoder.decode() + '\n\n')
  } finally {
    reader.releaseLock()
  }
}
