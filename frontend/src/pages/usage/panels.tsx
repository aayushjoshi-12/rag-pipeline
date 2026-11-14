import { ColumnChart } from '../../components/charts/ColumnChart'
import { HBars } from '../../components/charts/HBars'
import { Heatmap, WEEKDAYS, hourLabel } from '../../components/charts/Heatmap'
import { LineChart } from '../../components/charts/LineChart'
import {
  binLabel,
  compact,
  fmtDay,
  fmtDayLong,
  fmtInt,
  fmtMs,
  fmtPct,
  fmtUsd,
} from '../../components/charts/format'
import { Alert, Kpi } from '../../components/ui'
import type { Credits, DayUsage, Insights } from '../../types'
import { DataTable, TableView } from './DataTable'
import {
  burnPerDay,
  chats,
  cumulativeCost,
  cumulativeSaved,
  daysLeft,
  modelName,
  noHitSeries,
  totalUnpriced,
} from './model'

export interface PanelProps {
  days: DayUsage[]
  insights: Insights | null
  insightsError: string | null
}

const dayPoints = (days: { day: string }[]) =>
  days.map((d) => ({ key: d.day, label: fmtDay(d.day), full: fmtDayLong(d.day) }))
const dayCols = (days: DayUsage[]) =>
  days.map((d) => ({ key: d.day, label: fmtDay(d.day), full: fmtDayLong(d.day) }))

function Waiting({ error }: { error: string | null }) {
  return error ? (
    <Alert>{error}</Alert>
  ) : (
    <div className="chart-grid" aria-busy="true">
      {[0, 1].map((i) => (
        <div key={i} className="chart-card">
          <span className="skeleton" style={{ width: '40%' }} />
          <span className="skeleton skeleton-lg" style={{ width: '100%', height: '9rem' }} />
        </div>
      ))}
    </div>
  )
}

/** Credits left on the OpenRouter key as a budget bar, with the projected days at the 7-day burn. */
export function CreditsCard({ credits, days }: { credits: Credits | null; days: DayUsage[] }) {
  const burn = burnPerDay(days)
  let body
  if (credits === null) {
    body = <span className="skeleton" style={{ width: '60%' }} />
  } else if (!credits.available) {
    const why =
      credits.reason === 'provider_not_openrouter'
        ? 'The credit balance is only available for the OpenRouter provider.'
        : credits.reason === 'no_api_key'
          ? 'No OpenRouter API key is configured.'
          : 'The OpenRouter key balance could not be read right now.'
    body = (
      <p className="muted" data-testid="credits-unavailable">
        {why}
      </p>
    )
  } else if (credits.limit === null) {
    body = (
      <p data-testid="credits-remaining">
        <strong>{fmtUsd(credits.usage)}</strong> used. This key has no spending limit.
      </p>
    )
  } else {
    const remaining = credits.limit_remaining ?? Math.max(0, credits.limit - (credits.usage ?? 0))
    const used = Math.max(0, credits.limit - remaining)
    const frac = credits.limit > 0 ? Math.min(1, used / credits.limit) : 0
    const left = daysLeft(remaining, burn)
    const unpriced = totalUnpriced(days)
    body = (
      <>
        <div className="budget-figures">
          <span>
            <strong data-testid="credits-remaining">{fmtUsd(remaining)}</strong> left
          </span>
          <span className="muted">
            {fmtUsd(used)} used of {fmtUsd(credits.limit)}
          </span>
        </div>
        <div
          className="meter budget-meter"
          role="meter"
          aria-label="Credits used"
          aria-valuemin={0}
          aria-valuemax={credits.limit}
          aria-valuenow={used}
          aria-valuetext={`${fmtUsd(used)} of ${fmtUsd(credits.limit)} used`}
        >
          <span style={{ width: `${Math.max(used > 0 ? 0.8 : 0, frac * 100)}%` }} />
        </div>
        <p className="budget-forecast" data-testid="credits-forecast">
          {left === null
            ? 'Projection unavailable: no priced requests in the last 7 days.'
            : !Number.isFinite(left)
              ? 'No spend in the last 7 days, so the credits are not being used up.'
              : `About ${left >= 100 ? '100+' : left >= 10 ? Math.round(left) : left.toFixed(1)} days left at the current 7-day burn of ${fmtUsd(burn)} per day.`}
          {unpriced > 0 && left !== null
            ? ` ${chats(unpriced)} had no known cost, so the burn is a lower bound.`
            : ''}
        </p>
      </>
    )
  }
  return (
    <figure className="chart-card span-all" aria-label="Credits">
      <figcaption className="chart-head">
        <span className="chart-title">Credits</span>
        <span className="chart-caption">OpenRouter key balance</span>
      </figcaption>
      {body}
    </figure>
  )
}

export function CostPanel({
  days,
  insights,
  insightsError,
  credits,
}: PanelProps & { credits: Credits | null }) {
  const cost = days.map((d) => d.cost_usd)
  const anyCost = cost.some((c) => c !== null)
  const noCost =
    'No cost recorded in this period. Costs are reported by OpenRouter per request, or computed from LLM_PRICE_* settings.'
  return (
    <>
      <div className="chart-grid">
        <CreditsCard credits={credits} days={days} />
        <ColumnChart
          title="Spend per day"
          caption={
            totalUnpriced(days) > 0 ? 'days with unpriced chats are lower bounds' : undefined
          }
          series={[{ key: 'cost', label: 'Spend', color: 'var(--viz-1)' }]}
          data={days.map((d, i) => ({
            ...dayCols(days)[i]!,
            values: [d.cost_usd ?? 0],
            notes: [
              ...(d.cost_usd === null && d.requests > 0 ? ['cost unknown'] : []),
              ...(d.unpriced_requests > 0
                ? [
                    `${d.unpriced_requests} unpriced ${d.unpriced_requests === 1 ? 'chat' : 'chats'}`,
                  ]
                : []),
            ],
          }))}
          unit="usd"
          integer={false}
          valueFormat={fmtUsd}
          emptyText={anyCost ? 'Nothing was spent in this period.' : noCost}
        />
        <LineChart
          title="Cumulative spend"
          series={[
            {
              key: 'cum',
              label: 'Total spend',
              color: 'var(--viz-1)',
              values: cumulativeCost(days),
            },
          ]}
          points={dayPoints(days)}
          unit="usd"
          fill
          valueFormat={fmtUsd}
          emptyText={noCost}
        />
        <HBars
          title="Spend by model"
          caption={
            insights
              ? `average per answered chat: ${fmtUsd(insights.avg_cost_per_chat)}`
              : undefined
          }
          rows={(insights?.by_model ?? []).map((m) => ({
            key: m.model ?? '∅',
            label: modelName(m.model),
            value: m.cost_usd ?? 0,
            valueText: fmtUsd(m.cost_usd),
            sub: `${fmtInt(m.requests)} requests · ${compact(m.prompt_tokens + m.completion_tokens)} tokens${m.unpriced_requests ? ` · ${m.unpriced_requests} unpriced` : ''}`,
          }))}
          emptyText={insightsError ?? 'No chat requests in this period.'}
        />
        <ColumnChart
          title="LLM tokens per day"
          series={[
            { key: 'prompt', label: 'Prompt', color: 'var(--viz-1)' },
            { key: 'completion', label: 'Completion', color: 'var(--viz-2)' },
          ]}
          data={days.map((d, i) => ({
            ...dayCols(days)[i]!,
            values: [d.prompt_tokens, d.completion_tokens],
          }))}
          valueFormat={(n) => n.toLocaleString()}
          emptyText="No token usage recorded in this period (failed or token-less LLM calls report none)."
        />
      </div>
      <TableView>
        <DataTable
          label="usage by day"
          head={[
            { text: 'Day' },
            { text: 'Requests', num: true },
            { text: 'Errors', num: true },
            { text: 'Avg latency', num: true },
            { text: 'p95 latency', num: true },
            { text: 'Prompt tokens', num: true },
            { text: 'Completion tokens', num: true },
            { text: 'Cost', num: true },
            { text: 'Unpriced chats', num: true },
          ]}
          rows={days.map((d) => ({
            key: d.day,
            cells: [
              d.day,
              d.requests,
              d.errors,
              fmtMs(d.avg_ms),
              fmtMs(d.p95_ms),
              d.prompt_tokens.toLocaleString(),
              d.completion_tokens.toLocaleString(),
              fmtUsd(d.cost_usd),
              d.unpriced_requests,
            ],
          }))}
        />
        <DataTable
          label="spend by model"
          head={[
            { text: 'Model' },
            { text: 'Requests', num: true },
            { text: 'Tokens', num: true },
            { text: 'Cost', num: true },
            { text: 'Unpriced', num: true },
          ]}
          rows={(insights?.by_model ?? []).map((m) => ({
            key: m.model ?? '∅',
            cells: [
              modelName(m.model),
              m.requests,
              (m.prompt_tokens + m.completion_tokens).toLocaleString(),
              fmtUsd(m.cost_usd),
              m.unpriced_requests,
            ],
          }))}
        />
      </TableView>
    </>
  )
}

export function PerformancePanel({ days, insights, insightsError }: PanelProps) {
  const ins = insights
  return (
    <>
      <div className="chart-grid">
        <ColumnChart
          title="Requests per day"
          series={[
            { key: 'ok', label: 'Succeeded', color: 'var(--viz-1)' },
            { key: 'error', label: 'Failed', color: 'var(--viz-err)' },
          ]}
          data={days.map((d, i) => ({
            ...dayCols(days)[i]!,
            values: [d.requests - d.errors, d.errors],
          }))}
          emptyText="No requests in this period."
        />
        <LineChart
          title="Latency per day"
          caption="average and 95th percentile"
          series={[
            {
              key: 'avg',
              label: 'Average',
              color: 'var(--viz-1)',
              values: days.map((d) => d.avg_ms),
            },
            { key: 'p95', label: 'p95', color: 'var(--viz-2)', values: days.map((d) => d.p95_ms) },
          ]}
          points={dayPoints(days)}
          unit="ms"
          valueFormat={fmtMs}
          emptyText="No requests in this period."
        />
        {ins ? (
          <>
            <ColumnChart
              title="Latency distribution"
              caption={`requests per ${fmtMs(ins.latency_bucket_ms)} bucket`}
              adjacent
              edgeLabels
              series={[{ key: 'n', label: 'Requests', color: 'var(--viz-1)' }]}
              data={ins.latency_histogram.map((b) => ({
                key: String(b.lo),
                label: binLabel(b.lo, ins.latency_bucket_ms),
                full: `${fmtMs(b.lo)} to ${fmtMs(b.hi)}`,
                values: [b.count],
              }))}
              emptyText="No requests in this period."
            />
            <HBars
              title="Errors by reason"
              color="var(--viz-err)"
              rows={ins.errors_by_reason.map((r) => ({
                key: r.reason,
                label: r.reason,
                value: r.count,
                valueText: fmtInt(r.count),
              }))}
              emptyText="No errors in this period."
            />
            <div className="span-all">
              <Heatmap title="Requests by weekday and hour (UTC)" cells={ins.heatmap} />
            </div>
          </>
        ) : (
          <div className="span-all">
            <Waiting error={insightsError} />
          </div>
        )}
      </div>
      <TableView>
        <DataTable
          label="latency by day"
          head={[
            { text: 'Day' },
            { text: 'Requests', num: true },
            { text: 'Errors', num: true },
            { text: 'Avg latency', num: true },
            { text: 'p95 latency', num: true },
          ]}
          rows={days.map((d) => ({
            key: d.day,
            cells: [d.day, d.requests, d.errors, fmtMs(d.avg_ms), fmtMs(d.p95_ms)],
          }))}
        />
        <DataTable
          label="latency distribution"
          head={[{ text: 'From' }, { text: 'To' }, { text: 'Requests', num: true }]}
          rows={(ins?.latency_histogram ?? []).map((b) => ({
            key: String(b.lo),
            cells: [fmtMs(b.lo), fmtMs(b.hi), b.count],
          }))}
        />
        <DataTable
          label="errors by reason"
          head={[{ text: 'Reason' }, { text: 'Count', num: true }]}
          rows={(ins?.errors_by_reason ?? []).map((r) => ({
            key: r.reason,
            cells: [r.reason, r.count],
          }))}
        />
        <DataTable
          label="requests by weekday and hour"
          head={[{ text: 'Weekday' }, { text: 'Hour (UTC)' }, { text: 'Requests', num: true }]}
          rows={(ins?.heatmap ?? []).map((c) => ({
            key: `${c.weekday}-${c.hour}`,
            cells: [WEEKDAYS[c.weekday] ?? c.weekday, hourLabel(c.hour), c.count],
          }))}
        />
      </TableView>
    </>
  )
}

export function RagPanel({ insights, insightsError }: PanelProps) {
  if (!insights) return <Waiting error={insightsError} />
  const ins = insights
  const queries = ins.rag_days.reduce((a, d) => a + d.queries, 0)
  const noHit = ins.rag_days.reduce((a, d) => a + d.no_hit, 0)
  const saved = ins.reuse_days.reduce((a, d) => a + d.reused, 0)
  const embedded = ins.reuse_days.reduce((a, d) => a + d.embedded, 0)
  return (
    <>
      <div className="kpi-grid">
        <Kpi label="Answered queries" value={fmtInt(queries)} testId="tile-queries" />
        <Kpi
          label="No-hit rate"
          value={queries ? fmtPct(noHit / queries) : '—'}
          testId="tile-nohit"
          hint="queries that retrieved nothing"
        />
        <Kpi
          label="Documents never cited"
          value={fmtInt(ins.never_cited)}
          testId="tile-never-cited"
          hint={`of ${fmtInt(ins.total_documents)} in the library`}
        />
        <Kpi
          label="Embeddings saved"
          value={fmtInt(saved)}
          testId="tile-saved"
          hint={`${fmtInt(embedded)} chunks embedded`}
        />
      </div>
      <div className="chart-grid">
        <LineChart
          title="No-hit rate per day"
          caption="share of queries that retrieved nothing"
          series={[
            {
              key: 'r',
              label: 'No-hit rate',
              color: 'var(--viz-1)',
              values: noHitSeries(ins.rag_days),
            },
          ]}
          points={dayPoints(ins.rag_days)}
          unit="pct"
          valueFormat={(n) => fmtPct(n)}
          emptyText="No answered queries in this period."
        />
        <ColumnChart
          title="Hits per query"
          caption="chunks retrieved for each answered query"
          adjacent
          series={[{ key: 'n', label: 'Queries', color: 'var(--viz-1)' }]}
          data={ins.hits_histogram.map((b) => ({
            key: String(b.hits),
            label: String(b.hits),
            full: `${b.hits} ${b.hits === 1 ? 'hit' : 'hits'}`,
            values: [b.count],
          }))}
          emptyText="No answered queries in this period."
        />
        <HBars
          title="Most-cited documents"
          caption={`documents returned as answer sources · ${fmtInt(ins.never_cited)} never cited`}
          rows={ins.top_documents.map((d) => ({
            key: d.document_id,
            label: d.filename ?? 'deleted document',
            value: d.citations,
            valueText: fmtInt(d.citations),
          }))}
          emptyText="No chat answers have used a document in this period."
        />
        <ColumnChart
          title="Chunks reused vs embedded"
          caption="per day, successful ingests"
          series={[
            { key: 'reused', label: 'Reused (kept)', color: 'var(--viz-1)' },
            { key: 'embedded', label: 'Embedded (new)', color: 'var(--viz-2)' },
          ]}
          data={ins.reuse_days.map((d) => ({
            key: d.day,
            label: fmtDay(d.day),
            full: fmtDayLong(d.day),
            values: [d.reused, d.embedded],
          }))}
          emptyText="No ingests in this period."
        />
        <LineChart
          title="Embeddings saved"
          caption="cumulative chunks not re-embedded"
          series={[
            {
              key: 's',
              label: 'Saved',
              color: 'var(--viz-1)',
              values: saved ? cumulativeSaved(ins.reuse_days) : ins.reuse_days.map(() => null),
            },
          ]}
          points={dayPoints(ins.reuse_days)}
          integer
          fill
          valueFormat={(n) => fmtInt(n)}
          emptyText="No chunks were reused in this period."
        />
      </div>
      <TableView>
        <DataTable
          label="no-hit rate by day"
          head={[
            { text: 'Day' },
            { text: 'Queries', num: true },
            { text: 'No hit', num: true },
            { text: 'Rate', num: true },
          ]}
          rows={ins.rag_days.map((d) => ({
            key: d.day,
            cells: [d.day, d.queries, d.no_hit, d.queries ? fmtPct(d.no_hit / d.queries) : '—'],
          }))}
        />
        <DataTable
          label="hits per query"
          head={[{ text: 'Hits' }, { text: 'Queries', num: true }]}
          rows={ins.hits_histogram.map((b) => ({ key: String(b.hits), cells: [b.hits, b.count] }))}
        />
        <DataTable
          label="most-cited documents"
          head={[{ text: 'Document' }, { text: 'Citations', num: true }]}
          rows={ins.top_documents.map((d) => ({
            key: d.document_id,
            cells: [d.filename ?? 'deleted document', d.citations],
          }))}
        />
        <DataTable
          label="chunk reuse by day"
          head={[
            { text: 'Day' },
            { text: 'Ingests', num: true },
            { text: 'Reused', num: true },
            { text: 'Embedded', num: true },
          ]}
          rows={ins.reuse_days.map((d) => ({
            key: d.day,
            cells: [d.day, d.ingests, d.reused, d.embedded],
          }))}
        />
      </TableView>
    </>
  )
}

export function IngestPanel({ insights, insightsError }: PanelProps) {
  if (!insights) return <Waiting error={insightsError} />
  const ins = insights
  return (
    <>
      <div className="chart-grid">
        <HBars
          title="Ingest duration by mode"
          caption="average time of successful ingests"
          rows={ins.ingest_modes.map((m) => ({
            key: m.mode,
            label:
              m.mode === 'incremental'
                ? 'Incremental update'
                : m.mode === 'full'
                  ? 'Full ingest'
                  : m.mode,
            value: m.avg_ms,
            valueText: fmtMs(m.avg_ms),
            sub: `${fmtInt(m.runs)} ${m.runs === 1 ? 'run' : 'runs'} · p95 ${fmtMs(m.p95_ms)}`,
          }))}
          emptyText="No successful ingests in this period."
        />
        <HBars
          title="Ingest failures by reason"
          color="var(--viz-err)"
          rows={ins.ingest_failures.map((r) => ({
            key: r.reason,
            label: r.reason,
            value: r.count,
            valueText: fmtInt(r.count),
          }))}
          emptyText="No ingest failures in this period."
        />
      </div>
      <TableView>
        <DataTable
          label="ingest duration by mode"
          head={[
            { text: 'Mode' },
            { text: 'Runs', num: true },
            { text: 'Average', num: true },
            { text: 'p95', num: true },
          ]}
          rows={ins.ingest_modes.map((m) => ({
            key: m.mode,
            cells: [m.mode, m.runs, fmtMs(m.avg_ms), fmtMs(m.p95_ms)],
          }))}
        />
        <DataTable
          label="ingest failures by reason"
          head={[{ text: 'Reason' }, { text: 'Count', num: true }]}
          rows={ins.ingest_failures.map((r) => ({ key: r.reason, cells: [r.reason, r.count] }))}
        />
      </TableView>
    </>
  )
}
