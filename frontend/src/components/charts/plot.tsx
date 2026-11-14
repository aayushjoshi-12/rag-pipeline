import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react'

/** Measured width of the element behind `ref` (falls back when layout is unavailable, e.g. jsdom). */
export function useWidth(fallback = 640) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(fallback)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const w = Math.round(el.getBoundingClientRect().width)
      if (w > 0) setWidth(w)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return { ref, width }
}

/**
 * Hover/focus state for a row of `count` data positions, with a roving tab stop: one Tab stop for
 * the whole chart, arrow keys / Home / End move between points (a 90-day chart is not 90 tab stops).
 */
export function useActive(count: number) {
  const [active, setActive] = useState<number | null>(null)
  const [stop, setStop] = useState(0)
  const nodes = useRef<(SVGElement | null)[]>([])
  const register = useCallback(
    (i: number) => (el: SVGElement | null) => {
      nodes.current[i] = el
    },
    [],
  )
  const onKeyDown = (i: number) => (e: KeyboardEvent) => {
    const to =
      e.key === 'ArrowRight' || e.key === 'ArrowDown'
        ? Math.min(count - 1, i + 1)
        : e.key === 'ArrowLeft' || e.key === 'ArrowUp'
          ? Math.max(0, i - 1)
          : e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? count - 1
              : null
    if (to === null) return
    e.preventDefault()
    nodes.current[to]?.focus()
  }
  const focusProps = (i: number) => ({
    ref: register(i),
    tabIndex: i === Math.min(stop, count - 1) ? 0 : -1,
    onFocus: () => {
      setActive(i)
      setStop(i)
    },
    onBlur: () => setActive((a) => (a === i ? null : a)),
    onKeyDown: onKeyDown(i),
    onPointerMove: () => setActive(i),
  })
  const focusAt = (i: number) => nodes.current[i]?.focus()
  return { active, setActive, focusProps, focusAt }
}

export interface TipRow {
  /** series colour for the key; omit for a plain line */
  color?: string
  value: string
  label: string
}

/** One tooltip for every series at the hovered position. Text only (labels are untrusted data). */
export function Tooltip({
  x,
  width,
  title,
  rows,
  top = 0,
}: {
  x: number
  width: number
  title: string
  rows: TipRow[]
  top?: number
}) {
  const half = 96
  const left = Math.min(Math.max(x, half), Math.max(half, width - half))
  return (
    <div className="viz-tooltip" role="status" style={{ left, top }}>
      <div className="viz-tip-title">{title}</div>
      {rows.map((r, i) => (
        <div key={`${r.label}-${i}`} className="viz-tip-row">
          {r.color && <span className="viz-key" style={{ background: r.color }} />}
          <strong>{r.value}</strong>
          <span>{r.label}</span>
        </div>
      ))}
    </div>
  )
}

export function Legend({ items }: { items: { key: string; label: string; color: string }[] }) {
  if (items.length < 2) return null
  return (
    <ul className="viz-legend" aria-label="legend">
      {items.map((s) => (
        <li key={s.key}>
          <span className="viz-swatch" style={{ background: s.color }} />
          {s.label}
        </li>
      ))}
    </ul>
  )
}

export function EmptyPlot({ children }: { children: ReactNode }) {
  return <p className="viz-empty">{children}</p>
}

/** Horizontal gridlines + right-aligned y labels. */
export function YGrid({
  ticks,
  labels,
  y,
  x0,
  x1,
}: {
  ticks: number[]
  labels: string[]
  y: (v: number) => number
  x0: number
  x1: number
}) {
  return (
    <>
      {ticks.map((t, i) => (
        <g key={t}>
          <line className="viz-grid" x1={x0} x2={x1} y1={y(t)} y2={y(t)} />
          <text className="viz-tick" x={x0 - 8} y={y(t) + 4} textAnchor="end">
            {labels[i]}
          </text>
        </g>
      ))}
    </>
  )
}

/** Left margin wide enough for the widest y label (11px text). */
export const marginFor = (labels: string[]): number =>
  Math.max(28, Math.ceil(Math.max(0, ...labels.map((l) => l.length)) * 6.6) + 14)
