import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { compact, fmtMs, fmtUsd } from '../components/charts/format'
import { Alert, Kpi, PageHeader, TabPanel, Tabs } from '../components/ui'
import type { Credits, DayUsage, Insights } from '../types'
import { chats, totalCost, totalUnpriced } from './usage/model'
import { CachePanel } from './usage/CachePanel'
import { CostPanel, IngestPanel, PerformancePanel, RagPanel } from './usage/panels'

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

const TABS = [
  { id: 'cost', label: 'Cost' },
  { id: 'performance', label: 'Performance' },
  { id: 'rag', label: 'RAG health' },
  { id: 'ingest', label: 'Ingest' },
  { id: 'cache', label: 'Cache' },
]

export default function UsagePage() {
  const [params, setParams] = useSearchParams()
  const tab = TABS.some((t) => t.id === params.get('tab')) ? (params.get('tab') as string) : 'cost'
  const [days, setDays] = useState(14)
  const [kind, setKind] = useState('')
  const [data, setData] = useState<DayUsage[] | null>(null)
  const [insights, setInsights] = useState<Insights | null>(null)
  const [insightsError, setInsightsError] = useState<string | null>(null)
  const [credits, setCredits] = useState<Credits | null>(null)
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
    api
      .usageInsights(days, kind)
      .then((i) => {
        if (live) {
          setInsights(i)
          setInsightsError(null)
        }
      })
      .catch((e: unknown) => {
        if (live) {
          setInsights(null)
          setInsightsError(msg(e))
        }
      })
    return () => {
      live = false
    }
  }, [days, kind])

  useEffect(() => {
    let live = true
    api
      .usageCredits()
      .then((c) => {
        if (live) setCredits(c)
      })
      .catch(() => {
        if (live)
          setCredits({
            available: false,
            reason: 'unavailable',
            limit: null,
            limit_remaining: null,
            usage: null,
            is_free_tier: null,
            fetched_at: null,
          })
      })
    return () => {
      live = false
    }
  }, [])

  const requests = data?.reduce((a, d) => a + d.requests, 0) ?? 0
  const errors = data?.reduce((a, d) => a + d.errors, 0) ?? 0
  const tokens = data?.reduce((a, d) => a + d.prompt_tokens + d.completion_tokens, 0) ?? 0
  // request-weighted mean of the daily means
  const avg =
    data && requests ? data.reduce((a, d) => a + (d.avg_ms ?? 0) * d.requests, 0) / requests : null
  const spend = data ? totalCost(data) : null
  const unpriced = data ? totalUnpriced(data) : 0

  const panelProps = { days: data ?? [], insights, insightsError }

  return (
    <div className="stack">
      <PageHeader
        title="Usage"
        description="Cost, latency and retrieval health of the pipeline."
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
                <option value="search">Search</option>
                <option value="ingest">Ingest</option>
              </select>
            </label>
          </div>
        }
      />
      {error && <Alert>{error}</Alert>}
      {!data && !error && (
        <div className="kpi-grid" aria-busy="true">
          {[0, 1, 2, 3, 4].map((i) => (
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
            <Kpi label="Avg latency" value={fmtMs(avg)} testId="tile-latency" />
            <Kpi label="LLM tokens" value={compact(tokens)} testId="tile-tokens" />
            <Kpi
              label="LLM spend"
              value={fmtUsd(spend)}
              testId="tile-spend"
              hint={
                spend === null
                  ? 'no cost recorded'
                  : unpriced > 0
                    ? `${chats(unpriced)} with unknown cost`
                    : undefined
              }
            />
          </div>
          <Tabs
            tabs={TABS}
            value={tab}
            label="Usage sections"
            onChange={(id) => setParams(id === 'cost' ? {} : { tab: id }, { replace: true })}
          />
          <TabPanel id={tab}>
            {tab === 'cost' && <CostPanel {...panelProps} credits={credits} />}
            {tab === 'performance' && <PerformancePanel {...panelProps} />}
            {tab === 'rag' && <RagPanel {...panelProps} />}
            {tab === 'ingest' && <IngestPanel {...panelProps} />}
            {tab === 'cache' && <CachePanel days={days} />}
          </TabPanel>
        </>
      )}
    </div>
  )
}
