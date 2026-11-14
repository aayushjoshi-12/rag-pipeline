import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { StackedBars, compact } from '../components/StackedBars'
import type { DayUsage } from '../types'

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))
const ms = (v: number | null): string => (v === null ? '—' : `${Math.round(v).toLocaleString()} ms`)

export default function UsagePage() {
  const [days, setDays] = useState(14)
  const [kind, setKind] = useState('')
  const [data, setData] = useState<DayUsage[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .usage(days, kind)
      .then((d) => {
        if (live) {
          setData(d)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [days, kind])

  const requests = data?.reduce((a, d) => a + d.requests, 0) ?? 0
  const errors = data?.reduce((a, d) => a + d.errors, 0) ?? 0
  const tokens = data?.reduce((a, d) => a + d.prompt_tokens + d.completion_tokens, 0) ?? 0
  // request-weighted mean of the daily means
  const avg =
    data && requests ? data.reduce((a, d) => a + (d.avg_ms ?? 0) * d.requests, 0) / requests : null

  return (
    <div>
      <h1>Usage</h1>
      <div className="filters">
        <label>
          Period
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </label>
        <label>
          Type
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All</option>
            <option value="chat">Chat</option>
            <option value="ingest">Ingest</option>
          </select>
        </label>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {data && (
        <>
          <div className="tiles">
            <div className="tile">
              <div className="num" data-testid="tile-requests">
                {requests.toLocaleString()}
              </div>
              <div className="lbl">Requests</div>
            </div>
            <div className="tile">
              <div className="num" data-testid="tile-error-rate">
                {requests ? `${((errors / requests) * 100).toFixed(1)}%` : '—'}
              </div>
              <div className="lbl">Error rate</div>
            </div>
            <div className="tile">
              <div className="num" data-testid="tile-latency">
                {ms(avg)}
              </div>
              <div className="lbl">Avg latency</div>
            </div>
            <div className="tile">
              <div className="num" data-testid="tile-tokens">
                {compact(tokens)}
              </div>
              <div className="lbl">LLM tokens</div>
            </div>
          </div>
          <StackedBars
            title="Requests per day"
            emptyText="No requests in this period."
            series={[
              { key: 'ok', label: 'Succeeded', color: 'var(--viz-1)' },
              { key: 'error', label: 'Failed', color: 'var(--viz-err)' },
            ]}
            data={data.map((d) => ({ x: d.day, values: [d.requests - d.errors, d.errors] }))}
          />
          <StackedBars
            title="LLM tokens per day"
            emptyText="No token usage recorded in this period (failed or token-less LLM calls report none)."
            series={[
              { key: 'prompt', label: 'Prompt', color: 'var(--viz-1)' },
              { key: 'completion', label: 'Completion', color: 'var(--viz-2)' },
            ]}
            data={data.map((d) => ({ x: d.day, values: [d.prompt_tokens, d.completion_tokens] }))}
          />
          <details>
            <summary>Table view</summary>
            <table aria-label="usage by day">
              <thead>
                <tr>
                  <th>Day</th>
                  <th>Requests</th>
                  <th>Errors</th>
                  <th>Avg latency</th>
                  <th>p95 latency</th>
                  <th>Prompt tokens</th>
                  <th>Completion tokens</th>
                </tr>
              </thead>
              <tbody>
                {data.map((d) => (
                  <tr key={d.day}>
                    <td>{d.day}</td>
                    <td>{d.requests}</td>
                    <td>{d.errors}</td>
                    <td>{ms(d.avg_ms)}</td>
                    <td>{ms(d.p95_ms)}</td>
                    <td>{d.prompt_tokens.toLocaleString()}</td>
                    <td>{d.completion_tokens.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </div>
  )
}
