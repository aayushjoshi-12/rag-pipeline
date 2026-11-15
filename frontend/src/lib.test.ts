import { describe, expect, it } from 'vitest'
import { msg, toggle, when } from './lib'

describe('lib', () => {
  it('msg reads Error messages and stringifies the rest', () => {
    expect(msg(new Error('boom'))).toBe('boom')
    expect(msg('plain')).toBe('plain')
  })

  it('toggle adds a missing item and removes a present one without mutating', () => {
    const a = ['x']
    expect(toggle(a, 'y')).toEqual(['x', 'y'])
    expect(toggle(a, 'x')).toEqual([])
    expect(a).toEqual(['x'])
  })

  it('when treats a naive timestamp as UTC, keeps explicit offsets, and falls back', () => {
    const fmt = (iso: string) =>
      new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    expect(when('2026-11-14T10:00:00')).toBe(fmt('2026-11-14T10:00:00Z'))
    expect(when('2026-11-14T10:00:00+05:00')).toBe(fmt('2026-11-14T10:00:00+05:00'))
    expect(when('not a date')).toBe('not a date')
    expect(when(null)).toBe('')
    expect(when(null, 'never')).toBe('never')
  })
})
