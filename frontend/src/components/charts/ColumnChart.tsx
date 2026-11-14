import { scaleBand } from 'd3-scale'
import { ChartCard } from '../ui'
import type { Unit } from './format'
import { EmptyPlot, Legend, Tooltip, YGrid, marginFor, useActive, useWidth } from './plot'
import { makeYAxis, xTickIndices } from './scales'

export interface ColSeries {
  key: string
  label: string
  /** CSS colour (a var from the viz tokens) */
  color: string
}
export interface ColDatum {
  key: string
  /** short x-axis label */
  label: string
  /** full label for the tooltip and screen readers */
  full: string
  values: number[]
  /** extra tooltip lines without a value (e.g. "2 unpriced requests") */
  notes?: string[]
}

const H = 210
const M = { top: 10, right: 10, bottom: 28 }
const GAP = 2 // surface-coloured gap between stacked segments
const MAX_BAR = 24

function topRounded(x: number, y: number, w: number, h: number): string {
  const r = Math.min(4, h, w / 2)
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`
}

/**
 * Stacked column chart (also the histogram): thin bars with a 4px-rounded data end on a square
 * baseline, 2px gaps between segments, hairline grid, legend for 2+ series, per-bar tooltip on
 * hover and keyboard focus (one Tab stop, arrow keys move).
 */
export function ColumnChart({
  title,
  caption,
  series,
  data,
  unit = 'count',
  integer = true,
  valueFormat = (n) => n.toLocaleString(),
  emptyText = 'No data in this period.',
  adjacent = false,
  edgeLabels = false,
}: {
  title: string
  caption?: string
  series: ColSeries[]
  data: ColDatum[]
  unit?: Unit
  integer?: boolean
  valueFormat?: (n: number) => string
  emptyText?: string
  /** bars touch (histogram) instead of being thin columns */
  adjacent?: boolean
  /** x labels sit at the bar's left edge (numeric bins: the label is the bin start) */
  edgeLabels?: boolean
}) {
  const { ref, width } = useWidth()
  const { active, setActive, focusProps } = useActive(data.length)
  const totals = data.map((d) => d.values.reduce((a, b) => a + b, 0))
  const empty = data.length === 0 || totals.every((t) => t === 0)
  const axis = makeYAxis(Math.max(0, ...totals), [H - M.bottom, M.top], unit, { integer })
  const left = marginFor(axis.labels)
  const plotW = Math.max(40, width - left - M.right)
  const band = scaleBand<number>()
    .domain(data.map((_, i) => i))
    .range([left, left + plotW])
    .paddingInner(0)
  const slot = band.bandwidth()
  const bw = adjacent ? Math.max(1, slot - 2) : Math.min(MAX_BAR, slot * 0.7)
  const y = axis.scale
  const labelled = new Set(xTickIndices(data.length, plotW, adjacent ? 40 : 52))
  const cur = active !== null ? data[active] : undefined

  return (
    <ChartCard title={title} caption={caption} legend={<Legend items={series} />}>
      <div ref={ref} className="viz-wrap" onPointerLeave={() => setActive(null)}>
        {empty ? (
          <EmptyPlot>{emptyText}</EmptyPlot>
        ) : (
          <>
            <svg
              width={width}
              height={H}
              viewBox={`0 0 ${width} ${H}`}
              role="img"
              aria-label={title}
            >
              <YGrid ticks={axis.ticks} labels={axis.labels} y={y} x0={left} x1={left + plotW} />
              {data.map((d, i) => {
                const cx = (band(i) ?? 0) + slot / 2
                let acc = 0
                const segs = d.values.map((v, si) => ({ v, si })).filter((s) => s.v > 0)
                return (
                  <g key={d.key} className={active === i ? 'viz-bar is-active' : 'viz-bar'}>
                    {segs.map((s, k) => {
                      const top = y(acc + s.v)
                      const base = y(acc)
                      const h = Math.max(1, base - top - (k > 0 ? GAP : 0))
                      acc += s.v
                      const fill = series[s.si]?.color
                      return k === segs.length - 1 ? (
                        <path key={s.si} d={topRounded(cx - bw / 2, top, bw, h)} fill={fill} />
                      ) : (
                        <rect
                          key={s.si}
                          x={cx - bw / 2}
                          y={top}
                          width={bw}
                          height={h}
                          fill={fill}
                        />
                      )
                    })}
                    {labelled.has(i) && (
                      <text
                        className="viz-tick"
                        x={edgeLabels ? (band(i) ?? 0) : cx}
                        y={H - 8}
                        textAnchor={edgeLabels ? 'start' : 'middle'}
                      >
                        {d.label}
                      </text>
                    )}
                    <rect
                      className="viz-hit"
                      x={band(i)}
                      y={M.top}
                      width={slot}
                      height={H - M.bottom - M.top}
                      role="img"
                      aria-label={`${d.full}: ${series.map((s, si) => `${s.label} ${d.values[si] ?? 0}`).join(', ')}`}
                      {...focusProps(i)}
                    />
                  </g>
                )
              })}
              <line className="viz-axis" x1={left} x2={left + plotW} y1={y(0)} y2={y(0)} />
            </svg>
            {cur && active !== null && (
              <Tooltip
                x={(band(active) ?? 0) + slot / 2}
                width={width}
                title={cur.full}
                rows={[
                  ...series.map((s, si) => ({
                    color: s.color,
                    value: valueFormat(cur.values[si] ?? 0),
                    label: s.label,
                  })),
                  ...(cur.notes ?? []).map((n) => ({ value: '', label: n })),
                ]}
              />
            )}
          </>
        )}
      </div>
    </ChartCard>
  )
}
