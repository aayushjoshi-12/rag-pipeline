import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { HBars } from '../../components/charts/HBars'
import { fmtInt, fmtMs, fmtPct, fmtUsd } from '../../components/charts/format'
import { Alert, Kpi } from '../../components/ui'
import type { CacheKindStats, CacheStats } from '../../types'
import { DataTable, TableView } from './DataTable'

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))
const LAYERS: [string, string][] = [
  ['embedding', 'Query embedding'],
  ['retrieval', 'Retrieval result'],
  ['answer', 'Chat answer'],
]

function layerHits(s: CacheStats, layer: string): { hit: number; miss: number } {
  const a = s.traces.search.layers[layer] ?? { hit: 0, miss: 0 }
  const b = s.traces.chat.layers[layer] ?? { hit: 0, miss: 0 }
  return { hit: a.hit + b.hit, miss: a.miss + b.miss }
}

const rate = (k: CacheKindStats) => (k.hit_rate === null ? '—' : fmtPct(k.hit_rate))

export function CachePanel({ days }: { days: number }) {
  const [stats, setStats] = useState<CacheStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .cacheStats(days)
      .then((s) => {
        if (live) {
          setStats(s)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [days])

  if (error) return <Alert>{error}</Alert>
  if (!stats) return <p className="muted">Loading cache statistics…</p>
  const { search, chat } = stats.traces
  const avoided = search.embeddings_avoided + chat.embeddings_avoided
  return (
    <>
      {!stats.config.enabled && (
        <Alert tone="warning">The query cache is switched off (CACHE_ENABLED=false).</Alert>
      )}
      {stats.config.enabled && !stats.redis.reachable && (
        <Alert>
          Redis is not reachable; requests are served uncached. {stats.redis.error ?? ''}
        </Alert>
      )}
      <div className="kpi-grid">
        <Kpi label="Search hit rate" value={rate(search)} testId="tile-cache-search" />
        <Kpi label="Chat hit rate" value={rate(chat)} testId="tile-cache-chat" />
        <Kpi
          label="Avg hit vs miss (search)"
          value={`${fmtMs(search.avg_hit_ms)} / ${fmtMs(search.avg_miss_ms)}`}
          testId="tile-cache-latency"
        />
        <Kpi
          label="LLM calls avoided"
          value={fmtInt(chat.llm_calls_avoided)}
          testId="tile-cache-llm"
          hint={chat.saved_usd === null ? 'cost unknown' : `${fmtUsd(chat.saved_usd)} saved`}
        />
        <Kpi label="Embedding calls avoided" value={fmtInt(avoided)} testId="tile-cache-embed" />
      </div>
      <div className="chart-grid">
        <HBars
          title="Hits by layer"
          caption="requests answered from cache, per layer"
          rows={LAYERS.map(([id, label]) => {
            const l = layerHits(stats, id)
            return {
              key: id,
              label,
              value: l.hit,
              valueText: fmtInt(l.hit),
              sub: `${fmtInt(l.miss)} misses`,
            }
          })}
          emptyText="No cached requests in this period."
        />
        <HBars
          title="Entries in Redis"
          caption="keys per layer right now"
          rows={LAYERS.map(([id, label]) => ({
            key: id,
            label,
            value: stats.redis.keys[id] ?? 0,
            valueText: fmtInt(stats.redis.keys[id] ?? 0),
          }))}
          emptyText="Nothing cached."
        />
      </div>
      <TableView>
        <DataTable
          label="cache by request type"
          head={[
            { text: 'Type' },
            { text: 'Requests', num: true },
            { text: 'Hits', num: true },
            { text: 'Misses', num: true },
            { text: 'Bypassed', num: true },
            { text: 'Avg hit', num: true },
            { text: 'Avg miss', num: true },
          ]}
          rows={(['search', 'chat'] as const).map((kind) => {
            const k = stats.traces[kind]
            return {
              key: kind,
              cells: [
                kind,
                fmtInt(k.requests),
                fmtInt(k.hits),
                fmtInt(k.misses),
                fmtInt(k.bypassed),
                fmtMs(k.avg_hit_ms),
                fmtMs(k.avg_miss_ms),
              ],
            }
          })}
        />
      </TableView>
    </>
  )
}
