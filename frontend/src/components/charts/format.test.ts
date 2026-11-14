import { describe, expect, it } from 'vitest'
import {
  binLabel,
  compact,
  fmtDay,
  fmtDayLong,
  fmtInt,
  fmtMs,
  fmtPct,
  fmtUsd,
  formatTicks,
} from './format'
import { makeYAxis, maxOf, nearestIndex, xTickIndices } from './scales'

describe('number formatting', () => {
  it.each([
    [0, '0'],
    [999, '999'],
    [0.5, '0.5'],
    [1500, '1.5k'],
    [2_500_000, '2.5M'],
  ])('compact(%s) = %s', (v, want) => expect(compact(v)).toBe(want))

  it.each([
    [null, '—'],
    [undefined, '—'],
    [Number.NaN, '—'],
    [0, '$0.00'], // a known zero (free model) is NOT the unknown dash
    [0.0000114, '$0.0000114'],
    [0.0004286, '$0.0004286'],
    [0.0125, '$0.0125'],
    [0.5, '$0.50'],
    [2.5, '$2.50'],
    [1234.5, '$1,234.50'],
  ])('fmtUsd(%s) = %s', (v, want) => expect(fmtUsd(v)).toBe(want))

  it('keeps unknown cost distinct from zero', () => {
    expect(fmtUsd(null)).not.toBe(fmtUsd(0))
  })

  it.each([
    [null, '—'],
    [250, '250 ms'],
    [2611.4, '2,611 ms'],
    [12_400, '12.4 s'],
  ])('fmtMs(%s) = %s', (v, want) => expect(fmtMs(v)).toBe(want))

  it('formats percentages, integers and days', () => {
    expect(fmtPct(0.063)).toBe('6.3%')
    expect(fmtPct(null)).toBe('—')
    expect(fmtInt(1236.4)).toBe('1,236')
    expect(fmtDay('2026-11-14')).toBe('Nov 14')
    expect(fmtDayLong('2026-11-14')).toBe('Sat, Nov 14')
    expect(fmtDay('not-a-date')).toBe('not-a-date')
  })

  it('labels histogram bins in ms below a second and seconds above', () => {
    expect(binLabel(300, 100)).toBe('300')
    expect(binLabel(5000, 5000)).toBe('5s')
    expect(binLabel(2500, 500)).toBe('2500')
    expect(binLabel(7500, 2500)).toBe('7.5s')
  })
})

describe('formatTicks', () => {
  it('never rounds a fractional tick onto its neighbour (0, 0.5, 1 stay distinct)', () => {
    expect(formatTicks([0, 0.5, 1], 'count')).toEqual(['0', '0.5', '1.0'])
  })

  it('adds decimals only as far as needed so 1.05k and 1.1k differ', () => {
    const labels = formatTicks([1000, 1050, 1100, 1150], 'count')
    expect(new Set(labels).size).toBe(4)
    expect(labels).toEqual(['1.00k', '1.05k', '1.10k', '1.15k'])
  })

  it('uses whole numbers when the ticks are whole', () => {
    expect(formatTicks([0, 20000, 40000, 60000], 'count')).toEqual(['0', '20k', '40k', '60k'])
    expect(formatTicks([0, 1_000_000, 2_000_000], 'count')).toEqual(['0', '1M', '2M'])
  })

  it('formats money with at least cents and enough decimals for sub-cent steps', () => {
    expect(formatTicks([0, 0.005, 0.01, 0.015], 'usd')).toEqual([
      '$0',
      '$0.005',
      '$0.010',
      '$0.015',
    ])
    expect(formatTicks([0, 1, 2], 'usd')).toEqual(['$0', '$1.00', '$2.00'])
    expect(formatTicks([0, 0.00002, 0.00004], 'usd')).toEqual(['$0', '$0.00002', '$0.00004'])
  })

  it('switches latency ticks to seconds at 5 s and percentages to %', () => {
    expect(formatTicks([0, 10000, 20000], 'ms')).toEqual(['0', '10 s', '20 s'])
    expect(formatTicks([0, 250, 500], 'ms')).toEqual(['0', '250', '500'])
    expect(formatTicks([0, 0.05, 0.1], 'pct')).toEqual(['0', '5%', '10%'])
  })
})

describe('y axis for any data (labels are always distinct and the axis is never degenerate)', () => {
  const maxima = [
    0, 0.3, 0.9, 1, 1.4, 2, 3, 7, 9, 12, 99, 101, 430, 999, 1050, 1234, 9000, 123456, 2_400_000,
  ]
  for (const integer of [true, false]) {
    for (const unit of ['count', 'usd', 'ms', 'pct'] as const) {
      it(`unique labels for ${unit}${integer ? ' (integer)' : ''}`, () => {
        for (const m of maxima) {
          const axis = makeYAxis(m, [100, 0], unit, { integer })
          expect(new Set(axis.labels).size, `max ${m}: ${axis.labels.join(' ')}`).toBe(
            axis.labels.length,
          )
          expect(axis.ticks[0]).toBe(0)
          expect(axis.max).toBeGreaterThanOrEqual(
            Math.min(m, axis.ticks[axis.ticks.length - 1] ?? 0),
          )
          expect(axis.ticks.length).toBeGreaterThanOrEqual(2)
          expect(axis.scale(0)).toBe(100)
        }
      })
    }
  }

  it('gives an all-zero count axis 0..1, not fractional ticks', () => {
    const a = makeYAxis(0, [100, 0], 'count', { integer: true })
    expect(a.ticks).toEqual([0, 1])
    expect(a.labels).toEqual(['0', '1'])
  })

  it('keeps only whole-number ticks for counts, and covers the data', () => {
    const a = makeYAxis(3, [100, 0], 'count', { integer: true })
    expect(a.ticks.every(Number.isInteger)).toBe(true)
    expect(a.max).toBeGreaterThanOrEqual(3)
    const b = makeYAxis(1, [100, 0], 'count', { integer: true })
    expect(b.labels).toEqual(['0', '1'])
  })

  it('rounds the top up to a tick so the tallest bar never overflows', () => {
    const a = makeYAxis(0.0243, [100, 0], 'usd')
    expect(a.max).toBeGreaterThanOrEqual(0.0243)
    expect(a.scale(a.max)).toBe(0)
  })
})

describe('x ticks and helpers', () => {
  it('labels every day when there is room and thins them out otherwise, newest always shown', () => {
    expect(xTickIndices(7, 500)).toEqual([0, 1, 2, 3, 4, 5, 6])
    const dense = xTickIndices(30, 300)
    expect(dense[dense.length - 1]).toBe(29)
    const gaps = dense.slice(1).map((v, i) => v - (dense[i] ?? 0))
    expect(new Set(gaps).size).toBe(1) // evenly spaced
    expect(dense.length * 52).toBeLessThanOrEqual(300 + 52)
    expect(xTickIndices(0, 300)).toEqual([])
  })

  it('maxOf ignores nulls; nearestIndex snaps and clamps', () => {
    expect(maxOf([1, null, 5], [3])).toBe(5)
    expect(maxOf([null])).toBe(0)
    expect(nearestIndex(0, 0, 100, 5)).toBe(0)
    expect(nearestIndex(100, 0, 100, 5)).toBe(4)
    expect(nearestIndex(60, 0, 100, 5)).toBe(2) // 60% of 4 steps = 2.4 -> 2
    expect(nearestIndex(-50, 0, 100, 5)).toBe(0)
    expect(nearestIndex(500, 0, 100, 5)).toBe(4)
    expect(nearestIndex(10, 0, 100, 1)).toBe(0)
  })
})
