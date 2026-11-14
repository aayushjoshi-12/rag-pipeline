import { scaleSqrt } from 'd3-scale'
import type { KeyboardEvent } from 'react'
import type { HeatCell } from '../../types'
import { ChartCard } from '../ui'
import { compact } from './format'
import { EmptyPlot, Tooltip, useActive, useWidth } from './plot'

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const
const HOURS = 24
const CELL_H = 22
const LEFT = 36
const TOP = 4
const BOTTOM = 22

export const hourLabel = (h: number): string => `${String(h).padStart(2, '0')}:00`

/**
 * Requests by weekday (rows) and hour of day (columns), UTC. One hue, light to dark = few to
 * many requests (a sequential scale, sqrt-spaced so a few busy hours do not wash out the rest);
 * empty cells use a neutral. One Tab stop, arrow keys move across cells, each cell has a tooltip.
 */
export function Heatmap({
  title,
  caption,
  cells,
  emptyText = 'No requests in this period.',
}: {
  title: string
  caption?: string
  cells: HeatCell[]
  emptyText?: string
}) {
  const { ref, width } = useWidth()
  const grid = new Map(cells.map((c) => [c.weekday * HOURS + c.hour, c.count]))
  const peak = Math.max(0, ...cells.map((c) => c.count))
  const { active, setActive, focusProps, focusAt } = useActive(7 * HOURS)
  const plotW = Math.max(120, width - LEFT - 4)
  const cw = plotW / HOURS
  const H = TOP + 7 * CELL_H + BOTTOM
  const t = scaleSqrt()
    .domain([0, Math.max(1, peak)])
    .range([0, 100])
    .clamp(true)
  const fill = (n: number) =>
    n === 0
      ? 'var(--viz-heat-empty)'
      : `color-mix(in oklab, var(--viz-seq-hi) ${Math.round(8 + t(n) * 0.92)}%, var(--viz-seq-lo))`

  const onKey = (i: number) => (e: KeyboardEvent) => {
    const to =
      e.key === 'ArrowRight'
        ? i + 1
        : e.key === 'ArrowLeft'
          ? i - 1
          : e.key === 'ArrowDown'
            ? i + HOURS
            : e.key === 'ArrowUp'
              ? i - HOURS
              : null
    if (to === null) return
    e.preventDefault()
    if (to >= 0 && to < 7 * HOURS) focusAt(to)
  }
  const cur =
    active !== null
      ? { wd: Math.floor(active / HOURS), h: active % HOURS, n: grid.get(active) ?? 0 }
      : null

  return (
    <ChartCard title={title} caption={caption}>
      <div ref={ref} className="viz-wrap" onPointerLeave={() => setActive(null)}>
        {peak === 0 ? (
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
              {WEEKDAYS.map((d, r) => (
                <text
                  key={d}
                  className="viz-tick"
                  x={LEFT - 8}
                  y={TOP + r * CELL_H + CELL_H / 2 + 4}
                  textAnchor="end"
                >
                  {d}
                </text>
              ))}
              {[0, 6, 12, 18].map((h) => (
                <text key={h} className="viz-tick" x={LEFT + h * cw} y={H - 6}>
                  {String(h).padStart(2, '0')}
                </text>
              ))}
              {Array.from({ length: 7 * HOURS }, (_, i) => {
                const n = grid.get(i) ?? 0
                const wd = Math.floor(i / HOURS)
                const h = i % HOURS
                return (
                  <g key={i} className={active === i ? 'viz-cell is-active' : 'viz-cell'}>
                    <rect
                      x={LEFT + h * cw + 1}
                      y={TOP + wd * CELL_H + 1}
                      width={Math.max(1, cw - 2)}
                      height={CELL_H - 2}
                      rx={3}
                      fill={fill(n)}
                    />
                    <rect
                      className="viz-hit"
                      x={LEFT + h * cw}
                      y={TOP + wd * CELL_H}
                      width={cw}
                      height={CELL_H}
                      role="img"
                      aria-label={`${WEEKDAYS[wd]} ${hourLabel(h)} UTC: ${n} ${n === 1 ? 'request' : 'requests'}`}
                      {...focusProps(i)}
                      onKeyDown={onKey(i)}
                    />
                  </g>
                )
              })}
            </svg>
            <div className="heat-scale" aria-hidden="true">
              <span>Fewer</span>
              <span className="heat-ramp" />
              <span>More (max {compact(peak)})</span>
            </div>
            {cur && active !== null && (
              <Tooltip
                x={LEFT + cur.h * cw + cw / 2}
                width={width}
                top={TOP + (cur.wd + 1) * CELL_H}
                title={`${WEEKDAYS[cur.wd]} ${hourLabel(cur.h)} UTC`}
                rows={[
                  { value: cur.n.toLocaleString(), label: cur.n === 1 ? 'request' : 'requests' },
                ]}
              />
            )}
          </>
        )}
      </div>
    </ChartCard>
  )
}
