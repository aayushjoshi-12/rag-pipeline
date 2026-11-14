import { format } from 'd3-format'
import { utcFormat, utcParse } from 'd3-time-format'

export type Unit = 'count' | 'usd' | 'ms' | 'pct'

/** Short count for tiles and tooltips: 999, 1.5k, 2.5M (never "1000"). */
export const compact = (n: number): string =>
  n >= 1_000_000
    ? `${+(n / 1_000_000).toFixed(1)}M`
    : n >= 1000
      ? `${+(n / 1000).toFixed(1)}k`
      : Number.isInteger(n)
        ? String(n)
        : String(+n.toFixed(1))

const thousands = format(',d')
export const fmtInt = (n: number): string => thousands(Math.round(n))

/**
 * Money with ~3 significant digits, so a $0.0000114 call is not shown as $0.00.
 * `null` is "unknown" and renders as an em dash: unknown cost is never shown as zero.
 */
export function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (v === 0) return '$0.00'
  const abs = Math.abs(v)
  if (abs >= 1)
    return `${v < 0 ? '-' : ''}$${thousands(Math.trunc(abs))}.${abs.toFixed(2).split('.')[1]}`
  const digits = Math.min(8, Math.max(2, 2 - Math.floor(Math.log10(abs)) + 1))
  const fixed = abs.toFixed(digits).replace(/0+$/, '')
  const [i = '0', f = ''] = fixed.split('.')
  return `${v < 0 ? '-' : ''}$${i}.${f.padEnd(2, '0')}`
}

export function fmtMs(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (v >= 10_000) return `${(v / 1000).toFixed(1)} s`
  return `${Math.round(v).toLocaleString()} ms`
}

export const fmtPct = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`

const parseDay = utcParse('%Y-%m-%d')
const short = utcFormat('%b %-d')
const long = utcFormat('%a, %b %-d')
/** "2026-11-14" -> "Nov 14" (axis) */
export const fmtDay = (iso: string): string => {
  const d = parseDay(iso)
  return d ? short(d) : iso
}
/** "2026-11-14" -> "Sat, Nov 14" (tooltip, aria) */
export const fmtDayLong = (iso: string): string => {
  const d = parseDay(iso)
  return d ? long(d) : iso
}

/** Display divisor/suffix for an axis whose largest tick is `top`. */
function scaleFor(unit: Unit, top: number): { div: number; suffix: string; prefix: string } {
  switch (unit) {
    case 'usd':
      return { div: 1, suffix: '', prefix: '$' }
    case 'pct':
      return { div: 0.01, suffix: '%', prefix: '' }
    case 'ms':
      return top >= 5000
        ? { div: 1000, suffix: ' s', prefix: '' }
        : { div: 1, suffix: '', prefix: '' }
    default:
      return top >= 1_000_000
        ? { div: 1_000_000, suffix: 'M', prefix: '' }
        : top >= 1000
          ? { div: 1000, suffix: 'k', prefix: '' }
          : { div: 1, suffix: '', prefix: '' }
  }
}

/**
 * Axis labels for `ticks`: the fewest decimals at which every label is distinct AND exact.
 * (A fixed rounding rule once turned ticks 0, 0.5, 1 into "0, 1, 1", and 1.05k/1.1k into "1.1k" twice.)
 */
export function formatTicks(ticks: number[], unit: Unit): string[] {
  const top = Math.max(0, ...ticks.map(Math.abs))
  const { div, suffix, prefix } = scaleFor(unit, top)
  for (let d = unit === 'usd' ? 2 : 0; d <= 8; d++) {
    const labels = ticks.map((t) =>
      t === 0 ? `${prefix}0` : `${prefix}${(t / div).toFixed(d)}${suffix}`,
    )
    const exact = ticks.every(
      (t) =>
        Math.abs(Number((t / div).toFixed(d)) - t / div) < 1e-9 * Math.max(1, Math.abs(t / div)),
    )
    if (exact && new Set(labels).size === labels.length) return labels
  }
  return ticks.map(String)
}

/** Bin-start label: plain ms below 1 s buckets, seconds above (never "5,000" next to "10.0 s"). */
export const binLabel = (lo: number, step: number): string =>
  step >= 1000 ? `${+(lo / 1000).toFixed(1)}s` : String(lo)
