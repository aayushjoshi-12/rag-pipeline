import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearTokens, getTokens, onSessionLost, setTokens } from '../auth/session'
import { ApiError, api } from './client'

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
  clearTokens()
})

describe('api client', () => {
  it('GETs through the /api base and parses JSON', async () => {
    const fn = mockFetch(200, [{ id: '1' }])
    expect(await api.listDocuments()).toEqual([{ id: '1' }])
    expect(fn.mock.calls[0]?.[0]).toBe('/api/documents')
  })

  it('sends JSON for chat and a bearer token when set', async () => {
    const fn = mockFetch(200, { answer: 'a', sources: [] })
    setTokens({ access: 'tok', refresh: 'r' })
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

const json = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

describe('token refresh on 401', () => {
  it('refreshes once, retries with the new token and stores the new pair', async () => {
    setTokens({ access: 'old', refresh: 'ref1' })
    const fn = vi
      .fn()
      .mockResolvedValueOnce(json(401, { detail: 'invalid or expired token' }))
      .mockResolvedValueOnce(
        json(200, {
          access_token: 'new',
          refresh_token: 'ref2',
          token_type: 'bearer',
          expires_in: 1,
        }),
      )
      .mockResolvedValueOnce(json(200, [{ id: '1' }]))
    vi.stubGlobal('fetch', fn)

    expect(await api.listDocuments()).toEqual([{ id: '1' }])

    expect(fn).toHaveBeenCalledTimes(3)
    expect(fn.mock.calls[1]?.[0]).toBe('/api/auth/refresh')
    expect(JSON.parse((fn.mock.calls[1]?.[1] as RequestInit).body as string)).toEqual({
      refresh_token: 'ref1',
    })
    const retryHeaders = (fn.mock.calls[2]?.[1] as RequestInit).headers as Headers
    expect(retryHeaders.get('Authorization')).toBe('Bearer new')
    expect(getTokens()).toEqual({ access: 'new', refresh: 'ref2' })
  })

  it('shares one refresh between concurrent 401s', async () => {
    setTokens({ access: 'old', refresh: 'ref1' })
    let refreshes = 0
    const fn = vi.fn((url: string, init: RequestInit) => {
      if (url === '/api/auth/refresh') {
        refreshes += 1
        return Promise.resolve(
          json(200, {
            access_token: 'new',
            refresh_token: 'ref2',
            token_type: 'bearer',
            expires_in: 1,
          }),
        )
      }
      const auth = (init.headers as Headers).get('Authorization')
      return Promise.resolve(
        auth === 'Bearer new' ? json(200, []) : json(401, { detail: 'expired' }),
      )
    })
    vi.stubGlobal('fetch', fn)

    await Promise.all([api.listDocuments(), api.listUsers(), api.listRoles()])

    expect(refreshes).toBe(1)
  })

  it('drops the session and signals when the refresh fails', async () => {
    setTokens({ access: 'old', refresh: 'dead' })
    const lost = vi.fn()
    const off = onSessionLost(lost)
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(json(401, { detail: 'expired' }))
        .mockResolvedValueOnce(json(401, { detail: 'invalid or expired token' })),
    )

    await expect(api.listDocuments()).rejects.toMatchObject({ status: 401 })

    expect(getTokens()).toBeNull()
    expect(lost).toHaveBeenCalledTimes(1)
    off()
  })

  it('does not try to refresh failed logins or when signed out', async () => {
    const fn = vi.fn().mockResolvedValue(json(401, { detail: 'invalid credentials' }))
    vi.stubGlobal('fetch', fn)
    await expect(api.login('a@b.test', 'nope')).rejects.toMatchObject({ status: 401 })
    await expect(api.listDocuments()).rejects.toMatchObject({ status: 401 })
    expect(fn).toHaveBeenCalledTimes(2)
  })
})
