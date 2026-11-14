import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StackedBars, compact, niceMax } from './StackedBars'

const series = [
  { key: 'ok', label: 'Succeeded', color: 'var(--viz-1)' },
  { key: 'err', label: 'Failed', color: 'var(--viz-err)' },
]
const data = [
  { x: '2026-11-12', values: [10, 0] },
  { x: '2026-11-13', values: [6, 4] },
  { x: '2026-11-14', values: [0, 0] },
]

describe('niceMax / compact', () => {
  it.each([
    [0, 1],
    [1, 1],
    [1.5, 2],
    [3, 5],
    [7, 10],
    [12, 20],
    [430, 500],
    [9000, 10000],
  ])('niceMax(%s) = %s', (v, want) => expect(niceMax(v)).toBe(want))
  it.each([
    [0, '0'],
    [999, '999'],
    [0.5, '0.5'],
    [1, '1'],
    [1500, '1.5k'],
    [2_500_000, '2.5M'],
  ])('compact(%s) = %s', (v, want) => expect(compact(v)).toBe(want))
})

describe('StackedBars', () => {
  it('shows a legend for two series and none for one', () => {
    const { rerender } = render(<StackedBars title="Requests" series={series} data={data} />)
    expect(screen.getByLabelText('legend')).toHaveTextContent('SucceededFailed')
    rerender(<StackedBars title="Requests" series={series.slice(0, 1)} data={data} />)
    expect(screen.queryByLabelText('legend')).not.toBeInTheDocument()
  })

  it('draws one segment per non-zero value and nothing for empty days', () => {
    const { container } = render(<StackedBars title="Requests" series={series} data={data} />)
    // day1: 1 segment, day2: 2 segments, day3: 0
    expect(container.querySelectorAll('.viz-bar path, .viz-bar rect:not(.viz-hit)')).toHaveLength(3)
  })

  it('caps bar thickness at 24px and keeps a 2px gap between stacked segments', () => {
    const { container } = render(<StackedBars title="Requests" series={series} data={data} />)
    const bars = [...container.querySelectorAll('.viz-bar')]
    const rect = bars[1]!.querySelector('rect:not(.viz-hit)')!
    expect(Number(rect.getAttribute('width'))).toBeLessThanOrEqual(24)
    const lower = bars[1]!.querySelector('rect:not(.viz-hit)')!
    const upper = bars[1]!.querySelector('path')!
    const lowerTop = Number(lower.getAttribute('y'))
    const upperBottom = Number(/M[\d.]+,([\d.]+)V/.exec(upper.getAttribute('d')!)![1])
    expect(lowerTop - upperBottom).toBeCloseTo(2, 1) // the surface gap between the two fills
  })

  it('shows every series value in a tooltip on hover, with exact counts', () => {
    render(<StackedBars title="Requests" series={series} data={data} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.pointerMove(screen.getByLabelText('2026-11-13: Succeeded 6, Failed 4'))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('2026-11-13')
    expect(tip).toHaveTextContent('6Succeeded')
    expect(tip).toHaveTextContent('4Failed')
    fireEvent.pointerLeave(tip.parentElement!)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('exposes the same tooltip on keyboard focus', () => {
    render(<StackedBars title="Requests" series={series} data={data} />)
    const hit = screen.getByLabelText('2026-11-12: Succeeded 10, Failed 0')
    fireEvent.focus(hit)
    expect(screen.getByRole('status')).toHaveTextContent('10Succeeded')
    fireEvent.blur(hit)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders the empty message (not a blank plot) without any data', () => {
    const { container } = render(<StackedBars title="Empty" series={series} data={[]} />)
    expect(container.querySelectorAll('.viz-bar')).toHaveLength(0)
    expect(screen.getByText('No data in this period.')).toBeInTheDocument()
  })
})

describe('StackedBars empty and tick behaviour', () => {
  it('shows a message instead of an empty plot when every value is zero', () => {
    render(
      <StackedBars
        title="Tokens"
        series={series}
        data={[{ x: '2026-11-14', values: [0, 0] }]}
        emptyText="Nothing recorded."
      />,
    )
    expect(screen.getByText('Nothing recorded.')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Tokens' })).not.toBeInTheDocument()
  })

  it('never repeats a y-axis tick label (max 1 gives 0, 0.5, 1)', () => {
    const { container } = render(
      <StackedBars title="T" series={series} data={[{ x: '2026-11-14', values: [1, 0] }]} />,
    )
    const ticks = [...container.querySelectorAll('.viz-tick')].map((t) => t.textContent)
    const yTicks = ticks.filter((t) => !t!.includes('-'))
    expect(yTicks).toEqual(['0', '0.5', '1'])
    expect(new Set(yTicks).size).toBe(yTicks.length)
  })
})
