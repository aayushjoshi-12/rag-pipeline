import '@testing-library/jest-dom/vitest'
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AnswerText, healStreaming } from './AnswerText'
import type { Source } from '../types'

const sources = [1, 2, 3].map(
  (i) =>
    ({
      text: `passage ${i}`,
      document_id: `d${i}`,
      chunk_index: i,
      filename: `f${i}.md`,
      score: 0.1,
    }) as Source,
)

const view = (text: string, streaming = false, onCite = vi.fn()) => {
  const r = render(
    <AnswerText text={text} sources={sources} re={null} onCite={onCite} streaming={streaming} />,
  )
  return { ...r, onCite }
}

describe('markdown rendering', () => {
  it('renders bullet and numbered lists', () => {
    const { container } = view('Intro\n\n* one\n* two\n\n1. first\n2. second')
    expect(container.querySelectorAll('ul > li')).toHaveLength(2)
    expect(container.querySelectorAll('ol > li')).toHaveLength(2)
    expect(container.textContent).not.toContain('* one')
  })

  it('renders bold, italic, inline and fenced code, blockquote', () => {
    const { container } = view('**b** and *i* and `c`\n\n> quoted\n\n```py\nx = 1\n```')
    expect(container.querySelector('strong')?.textContent).toBe('b')
    expect(container.querySelector('em')?.textContent).toBe('i')
    expect(container.querySelector('p code')?.textContent).toBe('c')
    expect(container.querySelector('blockquote')?.textContent).toContain('quoted')
    expect(container.querySelector('pre code')?.textContent).toBe('x = 1\n')
  })

  it('renders a table inside a scroll wrapper', () => {
    const { container } = view('| a | b |\n|---|---|\n| 1 | 2 |')
    expect(container.querySelector('.md-table table th')?.textContent).toBe('a')
    expect(container.querySelectorAll('td')).toHaveLength(2)
  })
})

describe('citations inside markdown', () => {
  it('turns [n] into marker buttons in list items, bold, headings and tables', () => {
    const { container, onCite } = view(
      '# Title [1]\n\n* item [2]\n* **bold [3]**\n\npara [1, 2]\n\n| h |\n|---|\n| cell [3] |',
    )
    expect(container.querySelectorAll('li .cite')).toHaveLength(2)
    expect(container.querySelectorAll('strong .cite')).toHaveLength(1)
    expect(container.querySelectorAll('h4 .cite')).toHaveLength(1)
    expect(container.querySelectorAll('p .cite')).toHaveLength(2)
    expect(container.querySelectorAll('td .cite')).toHaveLength(1)
    const btn = container.querySelector('li .cite') as HTMLButtonElement
    expect(btn).toHaveAccessibleName('Source 2: f2.md, chunk 2')
    btn.click()
    expect(onCite).toHaveBeenCalledWith(2)
  })

  it('keeps citations in code spans, fences and invalid numbers as text; [1](url) is a link', () => {
    const { container } = view('`[1]` and [9] and [1](https://example.com/x)\n\n```\n[2]\n```')
    expect(container.querySelectorAll('.cite')).toHaveLength(0)
    expect(container.querySelector('code')?.textContent).toBe('[1]')
    expect(container.querySelector('pre')?.textContent).toContain('[2]')
    expect(container.textContent).toContain('[9]')
    const a = container.querySelector('a')!
    expect(a.textContent).toBe('1')
    expect(a.getAttribute('href')).toBe('https://example.com/x')
  })
})

describe('links', () => {
  it('never nests a marker button inside a link', () => {
    const { container } = view('[see [1] here](https://example.com)')
    expect(container.querySelector('a .cite')).toBeNull()
    expect(container.querySelector('a')?.textContent).toContain('[1]')
  })
})

describe('safety', () => {
  it('links open safely', () => {
    const { container } = view('[x](https://example.com)')
    const a = container.querySelector('a')!
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('renders no raw HTML, scripts, images, or dangerous URLs', () => {
    const { container } = view(
      [
        '<script>alert(1)</script>',
        '<img src=x onerror="alert(1)">',
        '<iframe src="https://evil.test"></iframe>',
        '<a href="javascript:alert(1)" onclick="x()">raw</a>',
        '[js](javascript:alert(1))',
        '[JS](JaVaScRiPt:alert(1))',
        '[data](data:text/html;base64,PHNjcmlwdD4=)',
        '![pic](https://evil.test/p.png "t")',
        '<div onmouseover="x()">hi</div>',
      ].join('\n\n'),
    )
    expect(container.querySelector('script, iframe, img, style, object, embed')).toBeNull()
    for (const el of container.querySelectorAll('*')) {
      for (const attr of el.getAttributeNames()) expect(attr.startsWith('on')).toBe(false)
    }
    for (const a of container.querySelectorAll('a')) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^(javascript|data):/i)
    }
    expect(container.innerHTML).not.toMatch(/javascript:|onerror|onclick|<script/i)
  })
})

describe('streaming', () => {
  const full =
    'Here is the summary [1]:\n\n* first **bold [2]** point\n* second `code [3]` point\n\n1. step one [1]\n2. step two\n\n```py\nprint("[2]")\nlong = 1\n```\n\nDone [1, 3].'

  it('is structurally stable at every chunk and never loses markers', () => {
    let lastCites = 0
    let lastItems = 0
    const r = render(<AnswerText text="" sources={sources} re={null} onCite={() => {}} streaming />)
    for (let n = 1; n <= full.length; n += 3) {
      r.rerender(
        <AnswerText
          text={full.slice(0, n)}
          sources={sources}
          re={null}
          onCite={() => {}}
          streaming
        />,
      )
      const c = r.container
      // never leaks raw markdown syntax for a construct that has been opened
      expect(c.querySelector('script, iframe, img')).toBeNull()
      const cites = c.querySelectorAll('.cite').length
      const items = c.querySelectorAll('li').length
      expect(cites).toBeGreaterThanOrEqual(lastCites)
      expect(items).toBeGreaterThanOrEqual(lastItems)
      // no half-typed marker text is ever visible
      expect(c.textContent).not.toMatch(/\[\d*,?\s*$/)
      // text inside an open fence never turns into a marker
      if (c.querySelector('pre')) expect(c.querySelector('pre .cite')).toBeNull()
      lastCites = cites
      lastItems = items
    }
    expect(r.container.querySelectorAll('.cite')).toHaveLength(5)
    expect(r.container.querySelectorAll('li')).toHaveLength(4)
    expect(r.container.querySelector('pre code')?.textContent).toContain('long = 1')
    // final streamed render equals the one-shot render
    const once = render(<AnswerText text={full} sources={sources} re={null} onCite={() => {}} />)
    expect(r.container.innerHTML.replace(/id="[^"]*"/g, '')).toBe(
      once.container.innerHTML.replace(/id="[^"]*"/g, ''),
    )
  })

  it('shows an unclosed bold/code/list/fence as formatted, not as raw syntax', () => {
    const b = view('a **half bold', true)
    expect(b.container.querySelector('strong')?.textContent).toBe('half bold')
    const c = view('use `partial', true)
    expect(c.container.querySelector('code')?.textContent).toBe('partial')
    const f = view('```\nopen fence [1]', true)
    expect(f.container.querySelector('pre')?.textContent).toContain('[1]')
    expect(f.container.querySelectorAll('.cite')).toHaveLength(0)
    const l = view('* one\n* tw', true)
    expect(l.container.querySelectorAll('li')).toHaveLength(2)
  })

  it('does not remount markers (popover state) when the answer re-renders', () => {
    const r = render(<AnswerText text="x [1]" sources={sources} re={null} onCite={() => {}} />)
    const before = r.container.querySelector('.cite')
    r.rerender(<AnswerText text="x [1] y" sources={sources} re={null} onCite={() => {}} />)
    expect(r.container.querySelector('.cite')).toBe(before)
  })
})

describe('healStreaming', () => {
  it('hides a trailing partial marker and closes dangling syntax', () => {
    expect(healStreaming('text [1')).toBe('text ')
    expect(healStreaming('text [1, ')).toBe('text ')
    expect(healStreaming('text [1]')).toBe('text [1]')
    expect(healStreaming('a **b')).toBe('a **b**')
    expect(healStreaming('a `b')).toBe('a `b`')
    expect(healStreaming('```\n**b')).toBe('```\n**b')
  })
})
