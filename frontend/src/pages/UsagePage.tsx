import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { StackedBars, compact } from '../components/StackedBars'
import { Alert, Kpi, PageHeader } from '../components/ui'
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
    <div className="stack">
      <PageHeader
        title="Usage"
        description="Request volume, latency and LLM token use over time."
        actions={
          <div className="toolbar toolbar-bare">
            <label className="field field-inline">
              <span className="field-label">Period</span>
              <select
                className="select"
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              >
                <option value={7}>Last 7 days</option>
                <option value={14}>Last 14 days</option>
                <option value={30}>Last 30 days</option>
                <option value={90}>Last 90 days</option>
              </select>
            </label>
            <label className="field field-inline">
              <span className="field-label">Type</span>
              <select className="select" value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="">All</option>
                <option value="chat">Chat</option>
                <option value="ingest">Ingest</option>
              </select>
            </label>
          </div>
        }
      />
      {error && <Alert>{error}</Alert>}
      {!data && !error && (
        <div className="kpi-grid" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="kpi">
              <span className="skeleton" style={{ width: '50%' }} />
              <span className="skeleton skeleton-lg" style={{ width: '70%' }} />
            </div>
          ))}
        </div>
      )}
      {data && (
        <>
          <div className="kpi-grid">
            <Kpi label="Requests" value={requests.toLocaleString()} testId="tile-requests" />
            <Kpi
              label="Error rate"
              value={requests ? `${((errors / requests) * 100).toFixed(1)}%` : '—'}
              testId="tile-error-rate"
            />
            <Kpi label="Avg latency" value={ms(avg)} testId="tile-latency" />
            <Kpi label="LLM tokens" value={compact(tokens)} testId="tile-tokens" />
          </div>
          <section className="usage-section" aria-labelledby="usage-traffic">
            <h2 id="usage-traffic" className="section-title">
              Traffic and tokens
            </h2>
            <div className="chart-grid">
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
                data={data.map((d) => ({
                  x: d.day,
                  values: [d.prompt_tokens, d.completion_tokens],
                }))}
              />
            </div>
          </section>
          <details className="card disclosure">
            <summary>Table view</summary>
            <div className="table-wrap">
              <table className="table" aria-label="usage by day">
                <thead>
                  <tr>
                    <th>Day</th>
                    <th className="num-col">Requests</th>
                    <th className="num-col">Errors</th>
                    <th className="num-col">Avg latency</th>
                    <th className="num-col">p95 latency</th>
                    <th className="num-col">Prompt tokens</th>
                    <th className="num-col">Completion tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((d) => (
                    <tr key={d.day}>
                      <td className="nowrap">{d.day}</td>
                      <td className="num-col">{d.requests}</td>
                      <td className="num-col">{d.errors}</td>
                      <td className="num-col nowrap">{ms(d.avg_ms)}</td>
                      <td className="num-col nowrap">{ms(d.p95_ms)}</td>
                      <td className="num-col">{d.prompt_tokens.toLocaleString()}</td>
                      <td className="num-col">{d.completion_tokens.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </div>
  )
}
