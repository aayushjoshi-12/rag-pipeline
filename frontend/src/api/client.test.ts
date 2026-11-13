import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, setAuthToken } from './client'

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => {
  vi.unstubAllGlobals()
  setAuthToken(null)
})

describe('api client', () => {
  it('GETs through the /api base and parses JSON', async () => {
    const fn = mockFetch(200, [{ id: '1' }])
    expect(await api.listDocuments()).toEqual([{ id: '1' }])
    expect(fn.mock.calls[0]?.[0]).toBe('/api/documents')
  })

  it('sends JSON for chat and a bearer token when set', async () => {
    const fn = mockFetch(200, { answer: 'a', sources: [] })
    setAuthToken('tok')
    await api.chat({ question: 'q' })
    const init = fn.mock.calls[0]?.[1] as RequestInit
    const headers = init.headers as Headers
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ question: 'q' }))
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('Authorization')).toBe('Bearer tok')
  })

  it('uploads multipart without forcing a content type', async () => {
    const fn = mockFetch(201, { document: {}, job: {} })
    await api.upload(new File(['x'], 'a.txt', { type: 'text/plain' }))
    const init = fn.mock.calls[0]?.[1] as RequestInit
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.headers as Headers).has('Content-Type')).toBe(false)
  })

  it('maps error responses to ApiError with the backend detail', async () => {
    mockFetch(413, { detail: 'File exceeds the limit.' })
    await expect(api.upload(new File(['x'], 'a.txt'))).rejects.toMatchObject({
      name: 'ApiError',
      status: 413,
      message: 'File exceeds the limit.',
    })
  })

  it('wraps network failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('boom')))
    const err = await api.health().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(0)
  })

  it('handles 204 delete', async () => {
    mockFetch(204, undefined)
    await expect(api.deleteDocument('x')).resolves.toBeUndefined()
  })
})
