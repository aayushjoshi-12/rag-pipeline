import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_PREFS,
  STORAGE_KEY,
  loadPrefs,
  prefsSummary,
  requestOverrides,
  savePrefs,
  scoreLabel,
} from './retrieval'

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('requestOverrides', () => {
  it('sends nothing for the server defaults', () => {
    expect(requestOverrides({ mode: '', fusion: '' })).toEqual({})
  })
  it('sends only the mode when fusion is left at default', () => {
    expect(requestOverrides({ mode: 'keyword', fusion: '' })).toEqual({ mode: 'keyword' })
    expect(requestOverrides({ mode: 'hybrid', fusion: '' })).toEqual({ mode: 'hybrid' })
  })
  it('sends fusion only together with hybrid', () => {
    expect(requestOverrides({ mode: 'hybrid', fusion: 'weighted' })).toEqual({
      mode: 'hybrid',
      fusion: 'weighted',
    })
    // a stale fusion value must not leak into other modes or into "server default"
    expect(requestOverrides({ mode: 'semantic', fusion: 'rrf' })).toEqual({ mode: 'semantic' })
    expect(requestOverrides({ mode: '', fusion: 'rrf' })).toEqual({})
  })
})

describe('scoreLabel', () => {
  it.each([
    ['distance', 0.4213, 'distance 0.42', /lower is closer/],
    ['bm25', 11.84, 'BM25 11.8', /higher is better/],
    ['ts_rank_cd', 0.3141, 'ts_rank_cd 0.31', /higher is better/],
    ['rrf', 0.03123, 'RRF 0.031', /higher is better/],
    ['weighted', 0.8125, 'weighted 0.813', /higher is better/],
  ])('%s', (kind, score, text, title) => {
    const l = scoreLabel({ score, score_kind: kind })
    expect(l.text).toBe(text)
    expect(l.title).toMatch(title)
  })
  it('treats a missing kind (older server) as a distance', () => {
    expect(scoreLabel({ score: 0.5 })).toMatchObject({ text: 'distance 0.50' })
  })
  it('does not claim a scale for an unknown kind', () => {
    const l = scoreLabel({ score: 0.5, score_kind: 'future_kind' })
    expect(l.text).toBe('score 0.50')
    expect(l.title).toMatch(/future_kind/)
    expect(l.title).not.toMatch(/lower is closer/)
  })
})

describe('preferences storage', () => {
  it('round-trips and summarises', () => {
    savePrefs({ mode: 'hybrid', fusion: 'rrf' })
    expect(loadPrefs()).toEqual({ mode: 'hybrid', fusion: 'rrf' })
    expect(prefsSummary({ mode: 'hybrid', fusion: 'rrf' })).toBe('Hybrid · RRF')
    expect(prefsSummary(DEFAULT_PREFS)).toBe('Server default')
    expect(prefsSummary({ mode: 'keyword', fusion: '' })).toBe('Keyword')
  })
  it('ignores corrupt or out-of-range saved values', () => {
    window.localStorage.setItem(STORAGE_KEY, '{"mode":"fuzzy","fusion":"borda"}')
    expect(loadPrefs()).toEqual(DEFAULT_PREFS)
    window.localStorage.setItem(STORAGE_KEY, 'not json')
    expect(loadPrefs()).toEqual(DEFAULT_PREFS)
  })
  it('survives unavailable storage on read and write', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(loadPrefs()).toEqual(DEFAULT_PREFS)
    expect(() => savePrefs({ mode: 'keyword', fusion: '' })).not.toThrow()
  })
})
