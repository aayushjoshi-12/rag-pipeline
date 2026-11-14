import { useState } from 'react'

export interface BarSeries {
  key: string
  label: string
  /** CSS colour (a var from the .viz scope) */
  color: string
}
export interface BarDatum {
  x: string
  values: number[]
}

/** Round an axis maximum up to 1, 2 or 5 × 10^k so gridlines land on readable numbers. */
export function niceMax(value: number): number {
  if (value <= 0) return 1
  const pow = 10 ** Math.floor(Math.log10(value))
  const n = value / pow
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow
}

export const compact = (n: number): string =>
  n >= 1_000_000
    ? `${+(n / 1_000_000).toFixed(1)}M`
    : n >= 1000
      ? `${+(n / 1000).toFixed(1)}k`
      : Number.isInteger(n)
        ? String(n)
        : String(+n.toFixed(1))

const W = 640
const H = 200
const M = { top: 8, right: 8, bottom: 24, left: 44 }
const GAP = 2 // surface-coloured gap between touching segments
const MAX_BAR = 24

function topRounded(x: number, y: number, w: number, h: number): string {
  const r = Math.min(4, h, w / 2)
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`
}

/** Stacked column chart: thin bars, 4px-rounded data end on a square baseline, 2px gaps,
 * hairline grid, legend for 2+ series, per-bar tooltip (hover and keyboard focus). */
export function StackedBars({
  title,
  series,
  data,
  format = compact,
  emptyText = 'No data in this period.',
}: {
  title: string
  series: BarSeries[]
  data: BarDatum[]
  format?: (n: number) => string
  emptyText?: string
}) {
  const [active, setActive] = useState<number | null>(null)
  const plotW = W - M.left - M.right
  const plotH = H - M.top - M.bottom
  const totals = data.map((d) => d.values.reduce((a, b) => a + b, 0))
  const max = niceMax(Math.max(0, ...totals))
  const band = data.length ? plotW / data.length : plotW
  const bw = Math.min(MAX_BAR, band * 0.7)
  const y = (v: number) => M.top + plotH - (v / max) * plotH
  const step = Math.max(1, Math.ceil(data.length / 7))
  const empty = totals.every((t) => t === 0)

  return (
    <figure className="viz">
      <figcaption>{title}</figcaption>
      {series.length > 1 && (
        <ul className="viz-legend" aria-label="legend">
          {series.map((s) => (
            <li key={s.key}>
              <span className="viz-swatch" style={{ background: s.color }} />
              {s.label}
            </li>
          ))}
        </ul>
      )}
      {empty ? (
        <p className="viz-empty">{emptyText}</p>
      ) : (
        <div className="viz-wrap" onPointerLeave={() => setActive(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
            {[0, 0.5, 1].map((f) => (
              <g key={f}>
                <line
                  className="viz-grid"
                  x1={M.left}
                  x2={W - M.right}
                  y1={y(max * f)}
                  y2={y(max * f)}
                />
                <text className="viz-tick" x={M.left - 6} y={y(max * f) + 4} textAnchor="end">
                  {format(max * f)}
                </text>
              </g>
            ))}
            {data.map((d, i) => {
              const cx = M.left + band * i + band / 2
              let acc = 0
              const segs = d.values.map((v, si) => ({ v, si })).filter((s) => s.v > 0)
              return (
                <g key={d.x} className={active === i ? 'viz-bar is-active' : 'viz-bar'}>
                  {segs.map((s, k) => {
                    const top = y(acc + s.v)
                    const base = y(acc)
                    const h = Math.max(1, base - top - (k > 0 ? GAP : 0))
                    const yy = k > 0 ? top : top
                    acc += s.v
                    const isTop = k === segs.length - 1
                    const fill = series[s.si]?.color
                    return isTop ? (
                      <path key={s.si} d={topRounded(cx - bw / 2, yy, bw, h)} fill={fill} />
                    ) : (
                      <rect key={s.si} x={cx - bw / 2} y={yy} width={bw} height={h} fill={fill} />
                    )
                  })}
                  {i % step === 0 && (
                    <text className="viz-tick" x={cx} y={H - 6} textAnchor="middle">
                      {d.x.slice(5)}
                    </text>
                  )}
                  <rect
                    className="viz-hit"
                    x={M.left + band * i}
                    y={M.top}
                    width={band}
                    height={plotH}
                    tabIndex={0}
                    role="img"
                    aria-label={`${d.x}: ${series.map((s, si) => `${s.label} ${d.values[si] ?? 0}`).join(', ')}`}
                    onPointerMove={() => setActive(i)}
                    onFocus={() => setActive(i)}
                    onBlur={() => setActive(null)}
                  />
                </g>
              )
            })}
            <line className="viz-axis" x1={M.left} x2={W - M.right} y1={y(0)} y2={y(0)} />
          </svg>
          {active !== null && data[active] && (
            <div
              className="viz-tooltip"
              role="status"
              style={{ left: `${((M.left + band * active + band / 2) / W) * 100}%` }}
            >
              <div className="viz-tip-title">{data[active].x}</div>
              {series.map((s, si) => (
                <div key={s.key} className="viz-tip-row">
                  <span className="viz-key" style={{ background: s.color }} />
                  <strong>{(data[active]?.values[si] ?? 0).toLocaleString()}</strong>
                  <span>{s.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </figure>
  )
}
