import { max as d3max } from 'd3-array'
import { scaleLinear, type ScaleLinear } from 'd3-scale'
import { formatTicks, type Unit } from './format'

export interface YAxis {
  scale: ScaleLinear<number, number>
  /** upper end of the domain (a tick value) */
  max: number
  ticks: number[]
  labels: string[]
}

/**
 * A zero-based value axis. `integer` keeps only whole-number ticks (counts: never "0.5 requests"),
 * all-zero data still gets a 0..1 axis, and labels are always distinct (see `formatTicks`).
 */
export function makeYAxis(
  rawMax: number,
  range: [number, number],
  unit: Unit,
  { integer = false, count = 4 }: { integer?: boolean; count?: number } = {},
): YAxis {
  const top = Number.isFinite(rawMax) && rawMax > 0 ? rawMax : 1
  const scale = scaleLinear()
    .domain([0, integer ? Math.max(1, Math.ceil(top)) : top])
    .nice(count)
  let ticks = scale.ticks(count)
  if (integer) ticks = ticks.filter((t) => Number.isInteger(t))
  const hi = scale.domain()[1] ?? 1
  if (ticks.length < 2) ticks = [0, hi]
  if (ticks[ticks.length - 1] !== hi && !integer) ticks = [...ticks, hi]
  scale.domain([0, Math.max(hi, ticks[ticks.length - 1] ?? hi)]).range(range)
  return { scale, max: scale.domain()[1] ?? 1, ticks, labels: formatTicks(ticks, unit) }
}

/** Indices of the x labels to draw so they never overlap; the newest point is always labelled. */
export function xTickIndices(n: number, plotWidth: number, labelWidth = 52): number[] {
  if (n <= 0) return []
  const step = Math.max(1, Math.ceil((n * labelWidth) / Math.max(1, plotWidth)))
  const out: number[] = []
  for (let i = 0; i < n; i++) if ((n - 1 - i) % step === 0) out.push(i)
  return out
}

/** Largest value across the given series arrays, ignoring nulls (0 when there is none). */
export function maxOf(...series: (number | null)[][]): number {
  return d3max(series.flat().filter((v): v is number => v !== null)) ?? 0
}

/** Index of the point nearest to pixel `x` for points evenly spread over [x0, x1]. */
export function nearestIndex(x: number, x0: number, x1: number, n: number): number {
  if (n <= 1) return 0
  const t = (x - x0) / (x1 - x0)
  return Math.min(n - 1, Math.max(0, Math.round(t * (n - 1))))
}
