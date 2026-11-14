import { Fragment, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Trace } from '../types'

const PAGE = 25
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

  const tokens = (t: Trace): string =>
    t.prompt_tokens === null && t.completion_tokens === null
      ? '—'
      : `${t.prompt_tokens ?? 0} / ${t.completion_tokens ?? 0}`

  return (
    <div>
      <h1>Traces</h1>
      <div className="filters">
        <label>
          Type
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value)
              setOffset(0)
            }}
          >
            <option value="">All</option>
            <option value="chat">Chat</option>
            <option value="ingest">Ingest</option>
          </select>
        </label>
        <label>
          Status
          <select
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
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {rows.length === 0 && !error ? (
        <p>No traces yet — they appear after chats and ingests.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Type</th>
              <th>Status</th>
              <th>User</th>
              <th>Model</th>
              <th>Duration</th>
              <th>Tokens (in / out)</th>
              <th>Hits</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <Fragment key={t.id}>
                <tr>
                  <td>
                    <button
                      className="linklike"
                      aria-expanded={open === t.id}
                      aria-label={`details ${t.id}`}
                      onClick={() => setOpen(open === t.id ? null : t.id)}
                    >
                      {new Date(t.created_at + 'Z').toLocaleString()}
                    </button>
                  </td>
                  <td>{t.kind}</td>
                  <td>
                    <span className={t.status === 'ok' ? 'badge badge-ok' : 'badge badge-failed'}>
                      {t.status === 'ok' ? '✓ ok' : '✕ error'}
                    </span>
                  </td>
                  <td>{t.user_email ?? '—'}</td>
                  <td>{t.model ?? '—'}</td>
                  <td>{t.duration_ms.toLocaleString()} ms</td>
                  <td>{tokens(t)}</td>
                  <td>{t.hits ?? '—'}</td>
                </tr>
                {open === t.id && (
                  <tr>
                    <td colSpan={8}>
                      <dl>
                        {t.error && (
                          <>
                            <dt>Error</dt>
                            <dd role="alert" className="error">
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
                            <dd>{t.document_id}</dd>
                          </>
                        )}
                        <dt>Details</dt>
                        <dd>
                          <code>{JSON.stringify(t.meta)}</code>
                        </dd>
                      </dl>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      <p>
        {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE, total)} of {total}{' '}
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
          Previous
        </button>{' '}
        <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
          Next
        </button>
      </p>
    </div>
  )
}
