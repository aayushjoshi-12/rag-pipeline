import { scaleLinear } from 'd3-scale'
import { ChartCard } from '../ui'
import { EmptyPlot } from './plot'

export interface HBarRow {
  key: string
  label: string
  value: number
  /** the exact value, directly labelled at the end of the bar */
  valueText: string
  /** secondary line (tokens, runs, p95...) */
  sub?: string
}

/**
 * Ranked horizontal bars: label above, a thin 4px-rounded bar, the exact value at the right.
 * Values are direct-labelled, so no tooltip is needed; long labels wrap instead of being cut.
 * Widths come from a d3 linear scale (zero-based, so a bar is always proportional to its value).
 */
export function HBars({
  title,
  caption,
  rows,
  color = 'var(--viz-1)',
  emptyText = 'No data in this period.',
}: {
  title: string
  caption?: string
  rows: HBarRow[]
  color?: string
  emptyText?: string
}) {
  const top = Math.max(0, ...rows.map((r) => r.value))
  const w = scaleLinear()
    .domain([0, top || 1])
    .range([0, 100])
  return (
    <ChartCard title={title} caption={caption}>
      {rows.length === 0 ? (
        <EmptyPlot>{emptyText}</EmptyPlot>
      ) : (
        <ol className="hbars" aria-label={title}>
          {rows.map((r) => (
            <li key={r.key} className="hbar">
              <div className="hbar-head">
                <span className="hbar-label" title={r.label}>
                  {r.label}
                </span>
                <strong className="hbar-value">{r.valueText}</strong>
              </div>
              <div className="hbar-track" aria-hidden="true">
                <span
                  className="hbar-fill"
                  style={{
                    width: `${r.value > 0 ? Math.max(1.5, w(r.value)) : 0}%`,
                    background: color,
                  }}
                />
              </div>
              {r.sub && <div className="hbar-sub">{r.sub}</div>}
            </li>
          ))}
        </ol>
      )}
    </ChartCard>
  )
}
