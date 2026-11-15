import config from '../vite.config'
import { describe, expect, it } from 'vitest'

describe('dev proxy', () => {
  const keys = Object.keys(config.server?.proxy ?? {})
  const matches = (path: string) =>
    keys.some((k) => (k.startsWith('^') ? new RegExp(k).test(path) : path.startsWith(k)))

  it('forwards API calls', () => {
    expect(matches('/api/v1/documents')).toBe(true)
  })

  it('does not capture the SPA route /api-keys (deep link and reload)', () => {
    expect(matches('/api-keys')).toBe(false)
  })
})
