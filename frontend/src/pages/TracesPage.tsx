import { Fragment, useEffect, useState } from 'react'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { Alert, Card, EmptyState, PageHeader } from '../components/ui'
import type { Trace } from '../types'
import { costCell, tokensCell, type UsageCell } from './traceUsage'

const PAGE = 25
const META_CHIPS: [key: string, label: string][] = [
  ['mode', 'mode'],
  ['fusion', 'fusion'],
  ['keyword_method', 'keyword method'],
  ['candidates', 'candidates'],
  ['top_k', 'top_k'],
  ['neighbors', 'neighbors'],
]

/** Known retrieval settings as readable chips; anything else stays reachable via "Raw". */
function MetaDetails({ meta }: { meta: Record<string, unknown> }) {
  const [raw, setRaw] = useState(false)
  const chips = META_CHIPS.filter(([k]) => {
    const v = meta[k]
    return v !== undefined && v !== null && typeof v !== 'object'
  })
  return (
    <>
      {chips.length > 0 && (
        <ul className="meta-chips" aria-label="retrieval settings">
          {chips.map(([k, label]) => (
            <li key={k} className="chip chip-static">
              <span className="muted">{label}</span> <strong>{String(meta[k])}</strong>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className="linklike small"
        aria-pressed={raw}
        onClick={() => setRaw((v) => !v)}
      >
        Raw
      </button>
      {(raw || chips.length === 0) && (
        <div>
          <code>{JSON.stringify(meta)}</code>
        </div>
      )}
    </>
  )
}

function UsageValue({ cell }: { cell: UsageCell }) {
  return (
    <span
      title={cell.title}
      className={cell.kind === 'known' ? undefined : 'muted'}
      data-usage={cell.kind}
    >
      {cell.text}
    </span>
  )
}

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export default function TracesPage() {
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<Trace[]>([])
  const [total, setTotal] = useState(0)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .listTraces({ kind, status, limit: PAGE, offset })
      .then((p) => {
        if (live) {
          setRows(p.items)
          setTotal(p.total)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [kind, status, offset])

  return (
    <div className="stack">
      <PageHeader
        title="Traces"
        description="Every chat, search and ingest request, with timing, token use, cost and errors."
      />
      <div className="toolbar">
        <label className="field field-inline">
          <span className="field-label">Type</span>
          <select
            className="select"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All</option>
            <option value="chat">Chat</option>
            <option value="search">Search</option>
            <option value="ingest">Ingest</option>
          </select>
        </label>
        <label className="field field-inline">
          <span className="field-label">Status</span>
          <select
            className="select"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All</option>
            <option value="ok">OK</option>
            <option value="error">Error</option>
          </select>
        </label>
      </div>
      {error && <Alert>{error}</Alert>}
      <Card flush>
        {rows.length === 0 && !error ? (
          <EmptyState icon="traces" title="No traces yet">
            They appear after chats, searches and ingests.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table traces-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>User</th>
                  <th>Model</th>
                  <th className="num-col">Duration</th>
                  <th className="num-col">Tokens (in / out)</th>
                  <th className="num-col">Cost</th>
                  <th className="num-col">Hits</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => (
                  <Fragment key={t.id}>
                    <tr className={open === t.id ? 'is-open' : undefined}>
                      <td className="nowrap">
                        <button
                          className="row-toggle num"
                          aria-expanded={open === t.id}
                          aria-label={`details ${t.id}`}
                          onClick={() => setOpen(open === t.id ? null : t.id)}
                        >
                          <Icon name="chevron" size={14} className="row-chevron" />
                          {new Date(t.created_at + 'Z').toLocaleString()}
                        </button>
                      </td>
                      <td>
                        <span className="tag">{t.kind}</span>
                      </td>
                      <td>
                        <span
                          className={t.status === 'ok' ? 'badge badge-ok' : 'badge badge-failed'}
                        >
                          {t.status === 'ok' ? '✓ ok' : '✕ error'}
                        </span>
                      </td>
                      <td className="cell-trunc muted" title={t.user_email ?? undefined}>
                        {t.user_email ?? '—'}
                      </td>
                      <td className="cell-trunc muted" title={t.model ?? undefined}>
                        {t.model ?? '—'}
                      </td>
                      <td className="num-col nowrap">{t.duration_ms.toLocaleString()} ms</td>
                      <td className="num-col nowrap">
                        <UsageValue cell={tokensCell(t)} />
                      </td>
                      <td className="num-col nowrap">
                        <UsageValue cell={costCell(t)} />
                      </td>
                      <td className="num-col">{t.hits ?? '—'}</td>
                    </tr>
                    {open === t.id && (
                      <tr className="detail-row">
                        <td colSpan={9}>
                          <dl className="detail-list">
                            {t.error && (
                              <>
                                <dt>Error</dt>
                                <dd role="alert" className="detail-error">
                                  {t.error}
                                </dd>
                              </>
                            )}
                            {t.question && (
                              <>
                                <dt>Question</dt>
                                <dd>{t.question}</dd>
                              </>
                            )}
                            {t.document_id && (
                              <>
                                <dt>Document</dt>
                                <dd className="mono">{t.document_id}</dd>
                              </>
                            )}
                            <dt>Details</dt>
                            <dd>
                              <MetaDetails meta={t.meta} />
                            </dd>
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="pager">
          <span className="muted num">
            {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE, total)} of {total}
          </span>
          <div className="pager-btns">
            <button
              className="btn btn-secondary btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
            >
              Previous
            </button>
            <button
              className="btn btn-secondary btn-sm"
              disabled={offset + PAGE >= total}
              onClick={() => setOffset(offset + PAGE)}
            >
              Next
            </button>
          </div>
        </div>
      </Card>
    </div>
  )
}
