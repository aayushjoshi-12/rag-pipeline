import type { Source } from './types'

/**
 * Pure helpers for the chat answer: citation markers, matched-term highlighting, relevance, grouping.
 * The prompt numbers the retrieved chunks `[1]..[n]` in the order of `sources[]` (neighbour context
 * chunks included), so marker `[k]` is `sources[k - 1]`.
 */

export type AnswerPart = { kind: 'text'; text: string } | { kind: 'cite'; n: number; raw: string }

// Fenced blocks (an unclosed fence runs to the end: the answer may still be streaming) and inline code.
const CODE = /```[\s\S]*?(?:```|$)|`[^`\n]*`/g
// [1], [12], [1,2], [1, 2]; not followed by "(" (a markdown link).
const MARKER = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?!\()/g

function pushText(parts: AnswerPart[], text: string) {
  if (!text) return
  const last = parts[parts.length - 1]
  if (last?.kind === 'text') last.text += text
  else parts.push({ kind: 'text', text })
}

function splitMarkers(text: string, count: number, parts: AnswerPart[]) {
  let at = 0
  for (const m of text.matchAll(MARKER)) {
    const start = m.index
    pushText(parts, text.slice(at, start))
    const nums = m[1]!.split(',').map((x) => Number(x.trim()))
    // Show each valid number as a marker; invalid ones ([0], [99]) stay as literal text.
    const valid = nums.filter((n) => n >= 1 && n <= count)
    if (valid.length === 0) {
      pushText(parts, m[0])
    } else if (valid.length === nums.length) {
      nums.forEach((n) => parts.push({ kind: 'cite', n, raw: `[${n}]` }))
    } else {
      pushText(parts, '[')
      nums.forEach((n, i) => {
        if (i > 0) pushText(parts, ', ')
        if (n >= 1 && n <= count) parts.push({ kind: 'cite', n, raw: String(n) })
        else pushText(parts, String(n))
      })
      pushText(parts, ']')
    }
    at = start + m[0].length
  }
  pushText(parts, text.slice(at))
}

/** Splits an answer into plain text and citation markers that point at a real source. */
export function parseAnswer(answer: string, sourceCount: number): AnswerPart[] {
  const parts: AnswerPart[] = []
  let at = 0
  for (const code of answer.matchAll(CODE)) {
    splitMarkers(answer.slice(at, code.index), sourceCount, parts)
    pushText(parts, code[0])
    at = code.index + code[0].length
  }
  splitMarkers(answer.slice(at), sourceCount, parts)
  return parts
}

const STOP = new Set(
  'the and for are but not you all any can had her was one our out has have his how its may new now see two who did get let she too use that this with from what when where which while will would about into than then them they their there these those been being does done each just like more most only other over some such very your'.split(
    ' ',
  ),
)

/** Words of the question worth highlighting in passages (lower-case, no stop words, capped). */
export function queryTerms(question: string): string[] {
  const words = question.toLowerCase().match(/[\p{L}\p{N}][\p{L}\p{N}_-]*/gu) ?? []
  const seen = new Set<string>()
  for (const w of words) {
    if (w.length >= 3 && !STOP.has(w)) seen.add(w)
    if (seen.size >= 12) break
  }
  return [...seen]
}

export const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export function termRegExp(terms: string[]): RegExp | null {
  return terms.length ? new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'giu') : null
}

export type TextPart = { text: string; hit: boolean }

/** Splits text into plain and matched pieces (terms are regex-escaped; the input is never trusted). */
export function highlight(text: string, re: RegExp | null): TextPart[] {
  if (!re) return [{ text, hit: false }]
  const out: TextPart[] = []
  let at = 0
  for (const m of text.matchAll(re)) {
    if (m[0] === '') continue
    if (m.index > at) out.push({ text: text.slice(at, m.index), hit: false })
    out.push({ text: m[0], hit: true })
    at = m.index + m[0].length
  }
  if (at < text.length) out.push({ text: text.slice(at), hit: false })
  return out
}

/** One-line preview: whitespace collapsed, windowed so the first matched term is visible. */
export function snippet(text: string, re: RegExp | null, max = 160): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  let start = 0
  if (re) {
    const first = new RegExp(re.source, 'iu').exec(flat)
    if (first && first.index > max / 2) start = first.index - Math.floor(max / 4)
  }
  const end = Math.min(flat.length, start + max)
  return `${start > 0 ? '…' : ''}${flat.slice(start, end)}${end < flat.length ? '…' : ''}`
}

export type Relevance = 'high' | 'medium' | 'low'

/** Non-numeric relevance from the rank among hits (scores are not comparable across modes). */
export function relevance(
  source: Pick<Source, 'rank' | 'is_hit'>,
  position: number,
  hits: number,
): { level: Relevance; rank: number; of: number } | null {
  if (source.is_hit === false) return null
  const rank = source.rank ?? position
  const of = Math.max(hits, 1)
  const third = Math.ceil(of / 3)
  const level: Relevance = rank <= third ? 'high' : rank <= third * 2 ? 'medium' : 'low'
  return { level, rank, of }
}

export interface NumberedSource {
  n: number
  source: Source
}
export interface SourceGroup {
  documentId: string
  filename: string
  items: NumberedSource[]
  hits: number
  context: number
}

/** Groups sources by document, in order of first appearance; `n` is the citation number. */
export function groupSources(sources: Source[]): SourceGroup[] {
  const groups = new Map<string, SourceGroup>()
  sources.forEach((source, i) => {
    let g = groups.get(source.document_id)
    if (!g) {
      g = {
        documentId: source.document_id,
        filename: source.filename,
        items: [],
        hits: 0,
        context: 0,
      }
      groups.set(source.document_id, g)
    }
    g.items.push({ n: i + 1, source })
    if (source.is_hit === false) g.context++
    else g.hits++
  })
  return [...groups.values()]
}

export const citationText = (s: Pick<Source, 'filename' | 'chunk_index'>) =>
  `${s.filename}, chunk ${s.chunk_index}`
