import { describe, expect, it } from 'vitest'
import {
  escapeRegExp,
  groupSources,
  highlight,
  parseAnswer,
  queryTerms,
  relevance,
  snippet,
  termRegExp,
} from './citations'
import type { Source } from './types'

const cites = (text: string, count = 5) =>
  parseAnswer(text, count).flatMap((p) => (p.kind === 'cite' ? [p.n] : []))
const plain = (text: string, count = 5) =>
  parseAnswer(text, count)
    .map((p) => (p.kind === 'text' ? p.text : `<${p.n}>`))
    .join('')

describe('parseAnswer', () => {
  it('finds single, adjacent and comma-separated markers', () => {
    expect(cites('a [1] b [2].')).toEqual([1, 2])
    expect(cites('a [1][2] b')).toEqual([1, 2])
    expect(cites('a [1,2] b [3, 4]')).toEqual([1, 2, 3, 4])
  })
  it('handles multi-digit markers exactly ([10] is not [1])', () => {
    expect(cites('see [10]', 12)).toEqual([10])
    expect(plain('see [10]', 5)).toBe('see [10]') // 10 > 5 sources: stays text
  })
  it('leaves markers that point at no source as plain text', () => {
    expect(plain('x [0] y [6]')).toBe('x [0] y [6]')
    expect(plain('x [1, 9] y')).toBe('x [<1>, 9] y')
    expect(plain('x [] [a] [1a] [ 1 ]')).toBe('x [] [a] [1a] [ 1 ]')
  })
  it('does not touch markdown links, inline code or code fences', () => {
    expect(cites('[1](http://x.test) and `a[1]` and ```\n[2]\n``` done [3]')).toEqual([3])
    expect(cites('streaming ```py\nx[1]')).toEqual([]) // unclosed fence while streaming
  })
  it('keeps a half-typed marker as text while streaming', () => {
    expect(plain('so far [1')).toBe('so far [1')
    expect(plain('so far [1]')).toBe('so far <1>')
  })
  it('round-trips the text outside markers', () => {
    expect(plain('A [1] B\n\nC [2][3] D')).toBe('A <1> B\n\nC <2><3> D')
    expect(parseAnswer('', 3)).toEqual([])
  })
})

describe('highlighting', () => {
  it('extracts useful terms: no stop words, short words, duplicates', () => {
    expect(queryTerms('What is the minimum credit score for a loan? Credit!')).toEqual([
      'minimum',
      'credit',
      'score',
      'loan',
    ])
    expect(
      queryTerms('x '.repeat(40) + Array.from({ length: 30 }, (_, i) => `word${i}`).join(' '))
        .length,
    ).toBe(12)
  })
  it('escapes regex metacharacters in terms', () => {
    const re = termRegExp(['c++', '(a|b)', '[x]', 'a.b'])!
    expect(() => highlight('c++ (a|b) [x] a.b', re)).not.toThrow()
    expect(
      highlight('c++ and axb', re)
        .filter((p) => p.hit)
        .map((p) => p.text),
    ).toEqual(['c++'])
    expect(escapeRegExp('a.b*')).toBe('a\\.b\\*')
    expect(termRegExp([])).toBeNull()
  })
  it('splits into matched and plain parts, case-insensitively', () => {
    const parts = highlight('The Credit score', termRegExp(['credit', 'score']))
    expect(parts).toEqual([
      { text: 'The ', hit: false },
      { text: 'Credit', hit: true },
      { text: ' ', hit: false },
      { text: 'score', hit: true },
    ])
  })
  it('windows the snippet around the first match', () => {
    const text = `${'filler '.repeat(60)}needle ${'tail '.repeat(60)}`
    const s = snippet(text, termRegExp(['needle']), 100)
    expect(s).toContain('needle')
    expect(s.startsWith('…')).toBe(true)
    expect(s.length).toBeLessThan(110)
    expect(snippet('short  text\n here', null)).toBe('short text here')
  })
})

const src = (over: Partial<Source>): Source => ({
  text: 't',
  document_id: 'a',
  chunk_index: 0,
  filename: 'a.md',
  score: 0.1,
  ...over,
})

describe('groupSources and relevance', () => {
  it('groups by document in order of first appearance and numbers by source order', () => {
    const g = groupSources([
      src({ document_id: 'a', chunk_index: 1 }),
      src({ document_id: 'b', filename: 'b.md' }),
      src({ document_id: 'a', chunk_index: 2, is_hit: false }),
      src({ document_id: 'a', chunk_index: 3 }),
    ])
    expect(g.map((x) => [x.filename, x.hits, x.context, x.items.map((i) => i.n)])).toEqual([
      ['a.md', 2, 1, [1, 3, 4]],
      ['b.md', 1, 0, [2]],
    ])
  })
  it('maps rank to a level, never a number, and nothing for context', () => {
    const lv = (rank: number, hits: number) => relevance({ rank }, rank, hits)?.level
    expect([1, 2, 3, 4, 5, 6].map((r) => lv(r, 6))).toEqual([
      'high',
      'high',
      'medium',
      'medium',
      'low',
      'low',
    ])
    expect(relevance({ is_hit: false }, 1, 3)).toBeNull()
    expect(relevance({}, 1, 1)?.level).toBe('high')
  })
})
