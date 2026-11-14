import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ColumnChart } from './ColumnChart'
import { HBars } from './HBars'
import { Heatmap } from './Heatmap'
import { LineChart } from './LineChart'
import { Tooltip } from './plot'

const series = [
  { key: 'ok', label: 'Succeeded', color: 'var(--viz-1)' },
  { key: 'err', label: 'Failed', color: 'var(--viz-err)' },
]
const col = (key: string, values: number[], notes?: string[]) => ({
  key,
  label: key.slice(5),
  full: `Day ${key}`,
  values,
  notes,
})
const data = [col('2026-11-12', [10, 0]), col('2026-11-13', [6, 4]), col('2026-11-14', [0, 0])]
const ticks = (c: HTMLElement) => [...c.querySelectorAll('.viz-tick')].map((t) => t.textContent)

describe('ColumnChart', () => {
  it('shows a legend for two series and none for one', () => {
    const { rerender } = render(<ColumnChart title="Requests" series={series} data={data} />)
    expect(screen.getByLabelText('legend')).toHaveTextContent('SucceededFailed')
    rerender(<ColumnChart title="Requests" series={series.slice(0, 1)} data={data} />)
    expect(screen.queryByLabelText('legend')).not.toBeInTheDocument()
  })

  it('draws one segment per non-zero value and nothing for empty days', () => {
    const { container } = render(<ColumnChart title="Requests" series={series} data={data} />)
    expect(container.querySelectorAll('.viz-bar path, .viz-bar rect:not(.viz-hit)')).toHaveLength(3)
  })

  it('caps bar thickness at 24px and keeps a 2px gap between stacked segments', () => {
    const { container } = render(<ColumnChart title="Requests" series={series} data={data} />)
    const bar = container.querySelectorAll('.viz-bar')[1]!
    const lower = bar.querySelector('rect:not(.viz-hit)')!
    const upper = bar.querySelector('path')!
    expect(Number(lower.getAttribute('width'))).toBeLessThanOrEqual(24)
    const upperBottom = Number(/M[\d.]+,([\d.]+)V/.exec(upper.getAttribute('d')!)![1])
    expect(Number(lower.getAttribute('y')) - upperBottom).toBeCloseTo(2, 1)
  })

  it('lets histogram bars touch (2px gap) instead of capping them at 24px', () => {
    const { container } = render(
      <ColumnChart
        title="H"
        adjacent
        series={series.slice(0, 1)}
        data={[col('2026-11-12', [5]), col('2026-11-13', [3])]}
      />,
    )
    const d = container.querySelector('.viz-bar path')!.getAttribute('d')!
    const x0 = Number(/^M([\d.]+),/.exec(d)![1])
    const x1 = Number(/Q([\d.]+),[\d.]+ [\d.]+,[\d.]+V/.exec(d)![1])
    expect(x1 - x0).toBeGreaterThan(24)
  })

  it('shows every series value in a tooltip on hover, with exact counts', () => {
    render(<ColumnChart title="Requests" series={series} data={data} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.pointerMove(screen.getByLabelText('Day 2026-11-13: Succeeded 6, Failed 4'))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('Day 2026-11-13')
    expect(tip).toHaveTextContent('6Succeeded')
    expect(tip).toHaveTextContent('4Failed')
    fireEvent.pointerLeave(tip.parentElement!)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('formats tooltip values and appends notes (e.g. unpriced requests)', () => {
    render(
      <ColumnChart
        title="Spend"
        series={series.slice(0, 1)}
        data={[col('2026-11-14', [0.0125], ['2 unpriced chats'])]}
        unit="usd"
        integer={false}
        valueFormat={(n) => `$${n}`}
      />,
    )
    fireEvent.focus(screen.getByLabelText(/Day 2026-11-14/))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('$0.0125Succeeded')
    expect(tip).toHaveTextContent('2 unpriced chats')
  })

  it('exposes the same tooltip on keyboard focus and clears it on blur', () => {
    render(<ColumnChart title="Requests" series={series} data={data} />)
    const hit = screen.getByLabelText('Day 2026-11-12: Succeeded 10, Failed 0')
    fireEvent.focus(hit)
    expect(screen.getByRole('status')).toHaveTextContent('10Succeeded')
    fireEvent.blur(hit)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('has one Tab stop and moves with the arrow keys, Home and End', () => {
    const { container } = render(<ColumnChart title="Requests" series={series} data={data} />)
    const hits = [...container.querySelectorAll<SVGRectElement>('.viz-hit')]
    expect(hits.map((h) => h.getAttribute('tabindex'))).toEqual(['0', '-1', '-1'])
    hits[0]!.focus()
    fireEvent.keyDown(hits[0]!, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(hits[1])
    expect(screen.getByRole('status')).toHaveTextContent('Day 2026-11-13')
    fireEvent.keyDown(hits[1]!, { key: 'End' })
    expect(document.activeElement).toBe(hits[2])
    fireEvent.keyDown(hits[2]!, { key: 'ArrowRight' }) // clamped at the end
    expect(document.activeElement).toBe(hits[2])
    fireEvent.keyDown(hits[2]!, { key: 'Home' })
    expect(document.activeElement).toBe(hits[0])
    fireEvent.keyDown(hits[0]!, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(hits[0])
    expect(hits.map((h) => h.getAttribute('tabindex'))).toEqual(['0', '-1', '-1'])
  })

  it('renders the empty message (not a blank plot) without data or with all zeros', () => {
    const { rerender } = render(<ColumnChart title="Empty" series={series} data={[]} />)
    expect(screen.getByText('No data in this period.')).toBeInTheDocument()
    rerender(
      <ColumnChart
        title="Tokens"
        series={series}
        data={[col('2026-11-14', [0, 0])]}
        emptyText="Nothing recorded."
      />,
    )
    expect(screen.getByText('Nothing recorded.')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Tokens' })).not.toBeInTheDocument()
  })

  it('never repeats a y-axis tick label (max 1 gives 0 and 1, never 0, 1, 1)', () => {
    const { container } = render(
      <ColumnChart title="T" series={series} data={[col('2026-11-14', [1, 0])]} />,
    )
    const y = ticks(container).filter((t) => t !== '11-14')
    expect(y).toEqual(['0', '1'])
  })

  it('labels fractional money ticks distinctly and without rounding them away', () => {
    const { container } = render(
      <ColumnChart
        title="Spend"
        series={series.slice(0, 1)}
        data={[col('2026-11-13', [0.0043]), col('2026-11-14', [0.0243])]}
        unit="usd"
        integer={false}
      />,
    )
    const y = ticks(container).filter((t) => t!.startsWith('$'))
    expect(new Set(y).size).toBe(y.length)
    expect(y[0]).toBe('$0')
    expect(y.slice(1).every((t) => Number(t!.slice(1)) > 0)).toBe(true)
  })

  it('puts histogram labels at the bin start when asked', () => {
    const { container } = render(
      <ColumnChart
        title="H"
        adjacent
        edgeLabels
        series={series.slice(0, 1)}
        data={[col('2026-11-12', [5]), col('2026-11-13', [3])]}
      />,
    )
    const t = container.querySelector('.viz-tick[text-anchor="start"]')
    expect(t).not.toBeNull()
  })
})

const pts = ['2026-11-12', '2026-11-13', '2026-11-14', '2026-11-15'].map((k) => ({
  key: k,
  label: k.slice(5),
  full: `Day ${k}`,
}))
const fmt = (n: number) => `${n} ms`

describe('LineChart', () => {
  const two = [
    {
      key: 'avg',
      label: 'Average',
      color: 'var(--viz-1)',
      values: [100, 200, 300, 400] as (number | null)[],
    },
    {
      key: 'p95',
      label: 'p95',
      color: 'var(--viz-2)',
      values: [150, 450, 500, 900] as (number | null)[],
    },
  ]

  it('draws one 2px line per series with a legend', () => {
    const { container } = render(
      <LineChart title="Latency" series={two} points={pts} valueFormat={fmt} />,
    )
    expect(container.querySelectorAll('.viz-line path')).toHaveLength(2)
    expect(screen.getByLabelText('legend')).toHaveTextContent('Averagep95')
  })

  it('lists every series in one tooltip with a crosshair and a marker per series', () => {
    const { container } = render(
      <LineChart title="Latency" series={two} points={pts} valueFormat={fmt} />,
    )
    fireEvent.pointerMove(screen.getByLabelText(/Day 2026-11-14/))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('Day 2026-11-14')
    expect(tip).toHaveTextContent('300 msAverage')
    expect(tip).toHaveTextContent('500 msp95')
    expect(container.querySelectorAll('.viz-crosshair circle')).toHaveLength(2)
    expect(container.querySelectorAll('.viz-crosshair line')).toHaveLength(1)
    fireEvent.pointerLeave(tip.parentElement!)
    expect(container.querySelector('.viz-crosshair')).toBeNull()
  })

  it('moves the crosshair with the keyboard (one Tab stop)', () => {
    const { container } = render(
      <LineChart title="Latency" series={two} points={pts} valueFormat={fmt} />,
    )
    const hits = [...container.querySelectorAll<SVGRectElement>('.viz-hit')]
    expect(hits.map((h) => h.getAttribute('tabindex'))).toEqual(['0', '-1', '-1', '-1'])
    hits[0]!.focus()
    fireEvent.keyDown(hits[0]!, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(hits[1])
    expect(screen.getByRole('status')).toHaveTextContent('450 msp95')
  })

  it('breaks the line at missing days instead of drawing them as zero', () => {
    const gap = [
      {
        key: 'a',
        label: 'A',
        color: 'var(--viz-1)',
        values: [10, null, 30, 40] as (number | null)[],
      },
    ]
    const { container } = render(
      <LineChart title="L" series={gap} points={pts} valueFormat={fmt} />,
    )
    const d = container.querySelector('.viz-line path')!.getAttribute('d')!
    expect(d.match(/M/g)).toHaveLength(2) // two separate segments
    fireEvent.pointerMove(screen.getByLabelText(/Day 2026-11-13/))
    expect(screen.getByRole('status')).toHaveTextContent('—A')
    expect(screen.getByLabelText('Day 2026-11-13: A no data')).toBeInTheDocument()
  })

  it('shows a dot for an isolated point (a one-point line is invisible)', () => {
    const lone = [
      {
        key: 'a',
        label: 'A',
        color: 'var(--viz-1)',
        values: [null, 5, null, null] as (number | null)[],
      },
    ]
    const { container } = render(
      <LineChart title="L" series={lone} points={pts} valueFormat={fmt} />,
    )
    expect(container.querySelectorAll('.viz-line circle')).toHaveLength(1)
  })

  it('fills under the first series only when asked', () => {
    const one = two.slice(0, 1)
    const { container, rerender } = render(
      <LineChart title="L" series={one} points={pts} valueFormat={fmt} />,
    )
    expect(container.querySelector('.viz-area')).toBeNull()
    rerender(<LineChart title="L" series={one} points={pts} valueFormat={fmt} fill />)
    expect(container.querySelector('.viz-area')).not.toBeNull()
  })

  it('shows the empty message when there is no data at all', () => {
    const none = [
      {
        key: 'a',
        label: 'A',
        color: 'var(--viz-1)',
        values: [null, null, null, null] as (number | null)[],
      },
    ]
    render(
      <LineChart
        title="L"
        series={none}
        points={pts}
        valueFormat={fmt}
        emptyText="Nothing here."
      />,
    )
    expect(screen.getByText('Nothing here.')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'L' })).not.toBeInTheDocument()
  })

  it('uses a distinct, whole-number axis for integer series', () => {
    const one = [
      { key: 'a', label: 'A', color: 'var(--viz-1)', values: [1, 1, 1, 1] as (number | null)[] },
    ]
    const { container } = render(
      <LineChart title="L" series={one} points={pts} valueFormat={fmt} integer />,
    )
    const y = ticks(container).filter((t) => !t!.includes('-'))
    expect(y).toEqual(['0', '1'])
  })

  it('labels percentages exactly', () => {
    const rate = [
      {
        key: 'a',
        label: 'A',
        color: 'var(--viz-1)',
        values: [0.05, 0.1, null, 0.15] as (number | null)[],
      },
    ]
    const { container } = render(
      <LineChart
        title="L"
        series={rate}
        points={pts}
        unit="pct"
        valueFormat={(n) => `${n * 100}%`}
      />,
    )
    const y = ticks(container).filter((t) => t!.endsWith('%') || t === '0')
    expect(new Set(y).size).toBe(y.length)
    expect(y).toContain('15%')
  })
})

describe('HBars', () => {
  const rows = [
    { key: 'a', label: 'big.pdf', value: 100, valueText: '100', sub: '3 runs' },
    { key: 'b', label: 'half.pdf', value: 50, valueText: '50' },
    { key: 'c', label: 'none.pdf', value: 0, valueText: '—' },
  ]
  const width = (el: Element) => (el as HTMLElement).style.width

  it('sizes bars proportionally from zero and direct-labels the exact values', () => {
    const { container } = render(<HBars title="Top" rows={rows} />)
    const fills = [...container.querySelectorAll('.hbar-fill')]
    expect(width(fills[0]!)).toBe('100%')
    expect(width(fills[1]!)).toBe('50%')
    expect(width(fills[2]!)).toBe('0%') // a zero (or unknown) value draws no bar
    expect(screen.getByText('3 runs')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('is zero-based even when every value is positive (50 is half of 100, not 0)', () => {
    const { container } = render(
      <HBars
        title="Top"
        rows={[
          { ...rows[0]!, value: 100 },
          { ...rows[1]!, value: 50 },
        ]}
      />,
    )
    const fills = [...container.querySelectorAll('.hbar-fill')]
    expect(width(fills[0]!)).toBe('100%')
    expect(width(fills[1]!)).toBe('50%')
  })

  it('keeps a tiny non-zero value visible', () => {
    const { container } = render(
      <HBars
        title="Top"
        rows={[
          { ...rows[0]!, value: 10000 },
          { ...rows[1]!, value: 1 },
        ]}
      />,
    )
    expect(parseFloat(width(container.querySelectorAll('.hbar-fill')[1]!))).toBeGreaterThanOrEqual(
      1.5,
    )
  })

  it('renders the empty message without rows', () => {
    render(<HBars title="Top" rows={[]} emptyText="Nope." />)
    expect(screen.getByText('Nope.')).toBeInTheDocument()
  })

  it('renders labels as text (untrusted data is never HTML)', () => {
    const { container } = render(
      <HBars
        title="Top"
        rows={[{ key: 'x', label: '<img src=x onerror=alert(1)>', value: 1, valueText: '1' }]}
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
  })
})

describe('Heatmap', () => {
  const cells = [
    { weekday: 0, hour: 9, count: 2 },
    { weekday: 4, hour: 17, count: 8 },
  ]
  const fills = (c: HTMLElement) =>
    [...c.querySelectorAll('.viz-cell')].map((g) => g.querySelector('rect')!.getAttribute('fill'))

  it('draws 7 x 24 cells with an accessible label for each', () => {
    const { container } = render(<Heatmap title="Heat" cells={cells} />)
    expect(container.querySelectorAll('.viz-hit')).toHaveLength(168)
    expect(screen.getByLabelText('Mon 09:00 UTC: 2 requests')).toBeInTheDocument()
    expect(screen.getByLabelText('Fri 17:00 UTC: 8 requests')).toBeInTheDocument()
    expect(screen.getByLabelText('Sun 23:00 UTC: 0 requests')).toBeInTheDocument()
    expect(container.querySelectorAll('.viz-tick')).toHaveLength(7 + 4) // weekdays + 00/06/12/18
  })

  it('uses one hue that gets stronger with the count, and a neutral for empty cells', () => {
    const { container } = render(<Heatmap title="Heat" cells={cells} />)
    const f = fills(container)
    expect(f[0]).toBe('var(--viz-heat-empty)')
    const low = f[0 * 24 + 9]!
    const high = f[4 * 24 + 17]!
    const pct = (s: string) => Number(/var\(--viz-seq-hi\) (\d+)%/.exec(s)![1])
    expect(low).toContain('var(--viz-seq-lo)')
    expect(pct(high)).toBeGreaterThan(pct(low))
    expect(pct(high)).toBe(100) // the busiest cell is the darkest step
  })

  it('shows a tooltip on hover and focus with the exact count', () => {
    render(<Heatmap title="Heat" cells={cells} />)
    fireEvent.pointerMove(screen.getByLabelText('Fri 17:00 UTC: 8 requests'))
    const tip = screen.getByRole('status')
    expect(tip).toHaveTextContent('Fri 17:00 UTC')
    expect(tip).toHaveTextContent('8requests')
    fireEvent.blur(screen.getByLabelText('Fri 17:00 UTC: 8 requests'))
    fireEvent.focus(screen.getByLabelText('Mon 09:00 UTC: 2 requests'))
    expect(screen.getByRole('status')).toHaveTextContent('2requests')
  })

  it('has one Tab stop; arrows move by hour and by weekday', () => {
    const { container } = render(<Heatmap title="Heat" cells={cells} />)
    const hits = [...container.querySelectorAll<SVGRectElement>('.viz-hit')]
    expect(hits.filter((h) => h.getAttribute('tabindex') === '0')).toHaveLength(1)
    hits[30]!.focus()
    fireEvent.keyDown(hits[30]!, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(hits[31])
    fireEvent.keyDown(hits[31]!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(hits[31 + 24])
    fireEvent.keyDown(hits[55]!, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(hits[31])
    fireEvent.keyDown(hits[31]!, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(hits[30])
    hits[0]!.focus()
    fireEvent.keyDown(hits[0]!, { key: 'ArrowUp' }) // off the top edge: stays
    expect(document.activeElement).toBe(hits[0])
  })

  it('shows the empty message when there are no requests', () => {
    render(<Heatmap title="Heat" cells={[]} emptyText="Quiet." />)
    expect(screen.getByText('Quiet.')).toBeInTheDocument()
  })
})

describe('Tooltip placement', () => {
  const left = (c: HTMLElement) =>
    parseFloat((c.querySelector('.viz-tooltip') as HTMLElement).style.left)
  it('keeps the tooltip inside the chart near either edge and under the pointer elsewhere', () => {
    const rows = [{ value: '1', label: 'x' }]
    const { container, rerender } = render(<Tooltip x={4} width={300} title="t" rows={rows} />)
    expect(left(container)).toBe(96) // clamped away from the left edge
    rerender(<Tooltip x={299} width={300} title="t" rows={rows} />)
    expect(left(container)).toBe(204) // clamped away from the right edge
    rerender(<Tooltip x={150} width={300} title="t" rows={rows} />)
    expect(left(container)).toBe(150)
    rerender(<Tooltip x={10} width={100} title="t" rows={rows} />)
    expect(left(container)).toBe(96) // narrower than the tooltip: never negative
  })
})
