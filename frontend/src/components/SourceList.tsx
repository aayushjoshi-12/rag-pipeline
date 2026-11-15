import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  citationText,
  groupSources,
  relevance,
  snippet,
  type NumberedSource,
  type Relevance,
} from '../citations'
import { scoreLabel } from '../retrieval'
import type { Source } from '../types'
import { Marked } from './AnswerText'
import { Icon } from './Icon'

const LEVELS: Record<Relevance, number> = { high: 3, medium: 2, low: 1 }

/** Three bars, no number: the rank among the hits, since scores are not comparable across modes. */
function Meter({ level, rank, of }: { level: Relevance; rank: number; of: number }) {
  const label = `Relevance: ${level} (ranked ${rank} of ${of})`
  return (
    <span className="rel" role="img" aria-label={label} title={label}>
      {[1, 2, 3].map((i) => (
        <i key={i} className={i <= LEVELS[level] ? 'on' : undefined} />
      ))}
    </span>
  )
}

function CopyCitation({ source }: { source: Source }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(citationText(source))
      setState('copied')
    } catch {
      setState('failed') // clipboard blocked (insecure origin, permissions)
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 1800)
  }
  return (
    <>
      <button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy()}>
        <Icon name={state === 'copied' ? 'check' : 'file'} size={14} />
        Copy citation
      </button>
      <span className="muted small" role="status">
        {state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy' : ''}
      </span>
    </>
  )
}

function SourceRow({
  item,
  all,
  hits,
  re,
  flash,
  canOpen,
  onJump,
}: {
  item: NumberedSource
  all: NumberedSource[]
  hits: number
  re: RegExp | null
  flash: boolean
  canOpen: boolean
  onJump: (n: number) => void
}) {
  const { n, source } = item
  const [open, setOpen] = useState(false)
  const body = useId()
  const context = source.is_hit === false
  const rel = relevance(source, n, hits)
  const label = scoreLabel(source)
  const neighbours = all.filter(
    (o) =>
      o.source.is_hit === false &&
      o.source.context_for?.document_id === source.document_id &&
      o.source.context_for.chunk_index === source.chunk_index,
  )
  const owner = context
    ? all.find(
        (o) =>
          o.source.is_hit !== false &&
          o.source.document_id === source.context_for?.document_id &&
          o.source.chunk_index === source.context_for.chunk_index,
      )
    : undefined
  return (
    <li
      id={`src-${n}`}
      tabIndex={-1}
      className={`src-row${context ? ' is-context' : ''}${flash ? ' is-flash' : ''}`}
    >
      <button
        type="button"
        className="src-toggle"
        aria-expanded={open}
        aria-controls={open ? body : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="source-n" aria-hidden="true">
          {n}
        </span>
        <span className="src-snip">
          <span className="sr-only">Source {n}: </span>
          <Marked text={snippet(source.text, re)} re={re} />
        </span>
        {rel ? (
          <Meter {...rel} />
        ) : (
          <span
            className="chip chip-static"
            title="Neighbouring chunk added for context (not scored)"
          >
            context
          </span>
        )}
        <Icon name="chevron" size={14} className="src-chev" />
      </button>
      {open && (
        <div id={body} className="src-body">
          <p className="src-text">
            <Marked text={source.text} re={re} />
          </p>
          <p className="src-pos muted small">
            Chunk {source.chunk_index}
            {owner && (
              <>
                {' · context for '}
                <button type="button" className="src-jump" onClick={() => onJump(owner.n)}>
                  source {owner.n}
                </button>
              </>
            )}
            {neighbours.length > 0 && (
              <>
                {' · neighbouring context: '}
                {neighbours.map((o, i) => (
                  <span key={o.n}>
                    {i > 0 && ', '}
                    <button type="button" className="src-jump" onClick={() => onJump(o.n)}>
                      source {o.n}
                    </button>
                  </span>
                ))}
              </>
            )}
          </p>
          <div className="src-actions">
            <CopyCitation source={source} />
            {canOpen && (
              <Link className="btn btn-secondary btn-sm" to={`/documents/${source.document_id}`}>
                Open document
              </Link>
            )}
          </div>
          <details className="src-details">
            <summary>Scoring details</summary>
            <dl className="src-dl">
              {!context && (
                <>
                  <dt>Score</dt>
                  <dd className="num" title={label.title}>
                    {label.text}
                  </dd>
                </>
              )}
              {rel && (
                <>
                  <dt>Rank</dt>
                  <dd className="num">
                    {rel.rank} of {rel.of}
                  </dd>
                </>
              )}
              {source.semantic_rank != null && (
                <>
                  <dt>Semantic rank</dt>
                  <dd className="num" title="Rank in the semantic (meaning) results">
                    semantic #{source.semantic_rank}
                  </dd>
                </>
              )}
              {source.keyword_rank != null && (
                <>
                  <dt>Keyword rank</dt>
                  <dd className="num" title="Rank in the keyword (exact words) results">
                    keyword #{source.keyword_rank}
                  </dd>
                </>
              )}
              {context && (
                <>
                  <dt>Kind</dt>
                  <dd>Added for context</dd>
                </>
              )}
            </dl>
          </details>
        </div>
      )}
    </li>
  )
}

/** Sources grouped by document: the filename once, then one collapsed line per passage. */
export function SourceList({
  sources,
  re,
  flash,
  canOpen,
  onJump,
}: {
  sources: Source[]
  re: RegExp | null
  flash: number | null
  canOpen: boolean
  onJump: (n: number) => void
}) {
  const groups = groupSources(sources)
  const all = groups.flatMap((g) => g.items)
  const hits = sources.filter((s) => s.is_hit !== false).length
  return (
    <div className="src-groups">
      {groups.map((g) => (
        <section key={g.documentId} className="src-group" aria-label={g.filename}>
          <h3 className="src-doc">
            <Icon name="file" size={14} />
            <span className="truncate" title={g.filename}>
              {g.filename}
            </span>
            <span className="muted num src-count">
              {g.hits} {g.hits === 1 ? 'passage' : 'passages'}
              {g.context > 0 && ` + ${g.context} context`}
            </span>
          </h3>
          <ul className="src-rows">
            {g.items.map((item) => (
              <SourceRow
                key={item.n}
                item={item}
                all={all}
                hits={hits}
                re={re}
                flash={flash === item.n}
                canOpen={canOpen}
                onJump={onJump}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
