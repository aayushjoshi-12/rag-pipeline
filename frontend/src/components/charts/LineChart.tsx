import { area as d3area, line as d3line } from 'd3-shape'
import { scalePoint } from 'd3-scale'
import { ChartCard } from '../ui'
import type { Unit } from './format'
import { EmptyPlot, Legend, Tooltip, YGrid, marginFor, useActive, useWidth } from './plot'
import { makeYAxis, maxOf, xTickIndices } from './scales'

export interface LineSeries {
  key: string
  label: string
  color: string
  /** one value per x position; null = no data (the line breaks, it is not drawn as zero) */
  values: (number | null)[]
}
export interface LinePoint {
  key: string
  label: string
  full: string
}

const H = 210
const M = { top: 12, right: 14, bottom: 28 }

/**
 * Line chart on one value axis (never dual-axis). 2px lines, markers only on hover/focus (and for
 * isolated points), a snapping crosshair, one tooltip listing every series, optional soft area
 * under the first series. One Tab stop; arrow keys move the crosshair.
 */
export function LineChart({
  title,
  caption,
  series,
  points,
  unit = 'count',
  integer = false,
  fill = false,
  valueFormat,
  emptyText = 'No data in this period.',
}: {
  title: string
  caption?: string
  series: LineSeries[]
  points: LinePoint[]
  unit?: Unit
  integer?: boolean
  fill?: boolean
  valueFormat: (n: number) => string
  emptyText?: string
}) {
  const { ref, width } = useWidth()
  const { active, setActive, focusProps } = useActive(points.length)
  const hasData = series.some((s) => s.values.some((v) => v !== null))
  const axis = makeYAxis(maxOf(...series.map((s) => s.values)), [H - M.bottom, M.top], unit, {
    integer,
  })
  const left = marginFor(axis.labels)
  const plotW = Math.max(40, width - left - M.right)
  const n = points.length
  // points sit at the centre of equal slots so the first/last point do not touch the axes
  const xs = scalePoint<number>()
    .domain(points.map((_, i) => i))
    .range([left, left + plotW])
    .padding(0.5)
  const slot = n > 1 ? plotW / n : plotW
  const px = (i: number) => xs(i) ?? left
  const y = axis.scale
  const labelled = new Set(xTickIndices(n, plotW))
  const path = (s: LineSeries) =>
    d3line<number | null>()
      .defined((v) => v !== null)
      .x((_, i) => px(i))
      .y((v) => y(v ?? 0))(s.values)

  const lead = series[0]
  const areaPath =
    fill && lead
      ? d3area<number | null>()
          .defined((v) => v !== null)
          .x((_, i) => px(i))
          .y0(y(0))
          .y1((v) => y(v ?? 0))(lead.values)
      : null
  const cur = active !== null ? points[active] : undefined

  return (
    <ChartCard title={title} caption={caption} legend={<Legend items={series} />}>
      <div ref={ref} className="viz-wrap" onPointerLeave={() => setActive(null)}>
        {!hasData || n === 0 ? (
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
              <line className="viz-axis" x1={left} x2={left + plotW} y1={y(0)} y2={y(0)} />
              {areaPath && lead && <path d={areaPath} fill={lead.color} className="viz-area" />}
              {series.map((s) => (
                <g key={s.key} className="viz-line">
                  <path d={path(s) ?? ''} stroke={s.color} />
                  {s.values.map(
                    (v, i) =>
                      v !== null &&
                      s.values[i - 1] == null &&
                      s.values[i + 1] == null && (
                        <circle key={i} cx={px(i)} cy={y(v)} r={3} fill={s.color} />
                      ),
                  )}
                </g>
              ))}
              {points.map(
                (p, i) =>
                  labelled.has(i) && (
                    <text key={p.key} className="viz-tick" x={px(i)} y={H - 8} textAnchor="middle">
                      {p.label}
                    </text>
                  ),
              )}
              {cur && active !== null && (
                <g className="viz-crosshair" pointerEvents="none">
                  <line x1={px(active)} x2={px(active)} y1={M.top} y2={H - M.bottom} />
                  {series.map((s) => {
                    const v = s.values[active]
                    return v == null ? null : (
                      <circle
                        key={s.key}
                        cx={px(active)}
                        cy={y(v)}
                        r={4.5}
                        fill={s.color}
                        className="viz-dot"
                      />
                    )
                  })}
                </g>
              )}
              {points.map((p, i) => (
                <rect
                  key={p.key}
                  className="viz-hit"
                  x={px(i) - slot / 2}
                  y={M.top}
                  width={slot}
                  height={H - M.bottom - M.top}
                  role="img"
                  aria-label={`${p.full}: ${series
                    .map(
                      (s) =>
                        `${s.label} ${s.values[i] == null ? 'no data' : valueFormat(s.values[i])}`,
                    )
                    .join(', ')}`}
                  {...focusProps(i)}
                />
              ))}
            </svg>
            {cur && active !== null && (
              <Tooltip
                x={px(active)}
                width={width}
                title={cur.full}
                rows={series.map((s) => ({
                  color: s.color,
                  value: s.values[active] == null ? '—' : valueFormat(s.values[active]),
                  label: s.label,
                }))}
              />
            )}
          </>
        )}
      </div>
    </ChartCard>
  )
}
