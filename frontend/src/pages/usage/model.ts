import type { DayUsage, Insights } from '../../types'

/** Sum of the known daily costs; null when none is known (unknown is not zero). */
export function totalCost(days: DayUsage[]): number | null {
  const known = days.filter((d) => d.cost_usd !== null)
  return known.length ? known.reduce((a, d) => a + (d.cost_usd ?? 0), 0) : null
}

export const totalUnpriced = (days: DayUsage[]): number =>
  days.reduce((a, d) => a + d.unpriced_requests, 0)

/** Running total of the daily cost; all null when no day has a known cost. */
export function cumulativeCost(days: DayUsage[]): (number | null)[] {
  if (days.every((d) => d.cost_usd === null)) return days.map(() => null)
  let acc = 0
  return days.map((d) => (acc += d.cost_usd ?? 0))
}

/** Average spend per day over the last (up to) 7 days; null when nothing is priced. */
export function burnPerDay(days: DayUsage[], window = 7): number | null {
  const last = days.slice(-window)
  if (last.every((d) => d.cost_usd === null)) return null
  return last.reduce((a, d) => a + (d.cost_usd ?? 0), 0) / window
}

/** Whole days of credit left at the current burn; null = cannot tell, Infinity = no spend. */
export function daysLeft(remaining: number | null, burn: number | null): number | null {
  if (remaining === null || burn === null) return null
  if (burn <= 0) return Number.POSITIVE_INFINITY
  return Math.max(0, remaining / burn)
}

/** Share of answered chat queries that retrieved nothing, per day (null = no queries that day). */
export const noHitSeries = (rag: Insights['rag_days']): (number | null)[] =>
  rag.map((d) => (d.queries > 0 ? d.no_hit / d.queries : null))

/** Running total of chunks reused instead of re-embedded. */
export function cumulativeSaved(reuse: Insights['reuse_days']): number[] {
  let acc = 0
  return reuse.map((d) => (acc += d.reused))
}

export const modelName = (m: string | null): string => m ?? 'unknown model'

export const chats = (n: number): string => `${n} ${n === 1 ? 'chat' : 'chats'}`
