import { describe, expect, it } from 'vitest'
import type { DayUsage } from '../../types'
import {
  burnPerDay,
  chats,
  cumulativeCost,
  cumulativeSaved,
  daysLeft,
  noHitSeries,
  totalCost,
  totalUnpriced,
} from './model'

const d = (cost: number | null, over: Partial<DayUsage> = {}): DayUsage => ({
  day: '2026-11-14',
  requests: 1,
  errors: 0,
  avg_ms: 1,
  p95_ms: 1,
  prompt_tokens: 0,
  completion_tokens: 0,
  cost_usd: cost,
  unpriced_requests: 0,
  ...over,
})

describe('cost model', () => {
  it('totals known costs and keeps "nothing known" as null, not 0', () => {
    expect(totalCost([d(0.5), d(null), d(0.25)])).toBe(0.75)
    expect(totalCost([d(null), d(null)])).toBeNull()
    expect(totalCost([d(0), d(null)])).toBe(0) // a known zero is a number
    expect(totalCost([])).toBeNull()
    expect(totalUnpriced([d(1, { unpriced_requests: 2 }), d(null, { unpriced_requests: 3 })])).toBe(
      5,
    )
  })

  it('accumulates cost, skipping unknown days, and is all-null with no known cost', () => {
    expect(cumulativeCost([d(1), d(null), d(2), d(0)])).toEqual([1, 1, 3, 3])
    expect(cumulativeCost([d(null), d(null)])).toEqual([null, null])
  })

  it('burns the last 7 days only, averaged over 7', () => {
    const days = [d(100), d(100), ...Array.from({ length: 7 }, () => d(0.7))]
    expect(burnPerDay(days)).toBeCloseTo(0.7)
    expect(burnPerDay([d(0.7), d(null)])).toBeCloseTo(0.1) // short history still divides by the window
    expect(burnPerDay([d(null), d(null)])).toBeNull()
  })

  it('pluralises chats', () => {
    expect([chats(0), chats(1), chats(2)]).toEqual(['0 chats', '1 chat', '2 chats'])
  })

  it('projects days left from remaining credit and burn', () => {
    expect(daysLeft(2.5, 0.5)).toBe(5)
    expect(daysLeft(2.5, 0)).toBe(Number.POSITIVE_INFINITY)
    expect(daysLeft(0, 0.5)).toBe(0)
    expect(daysLeft(null, 0.5)).toBeNull()
    expect(daysLeft(2.5, null)).toBeNull()
  })

  it('computes no-hit rate per day (null without queries) and cumulative savings', () => {
    expect(
      noHitSeries([
        { day: 'a', queries: 4, no_hit: 1 },
        { day: 'b', queries: 0, no_hit: 0 },
        { day: 'c', queries: 2, no_hit: 2 },
      ]),
    ).toEqual([0.25, null, 1])
    expect(
      cumulativeSaved([
        { day: 'a', ingests: 1, reused: 5, embedded: 1 },
        { day: 'b', ingests: 0, reused: 0, embedded: 0 },
        { day: 'c', ingests: 2, reused: 7, embedded: 0 },
      ]),
    ).toEqual([5, 5, 12])
  })
})
