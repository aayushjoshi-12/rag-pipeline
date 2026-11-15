import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { citationText, highlight, parseAnswer, snippet } from '../citations'
import type { Source } from '../types'

/** Matched terms rendered as <mark>. */
export function Marked({ text, re }: { text: string; re: RegExp | null }): ReactNode {
  return (
    <>
      {highlight(text, re).map((p, i) =>
        p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>,
      )}
    </>
  )
}

/** Citation marker: a real button. Hover or focus shows a text preview, Esc closes it, activating it
 * jumps to the source row. The preview is a tooltip (text only), the row below is the full detail. */
function Cite({
  n,
  source,
  re,
  onCite,
}: {
  n: number
  source: Source
  re: RegExp | null
  onCite: (n: number) => void
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const tip = useRef<HTMLSpanElement>(null)
  const id = useId()

  useLayoutEffect(() => {
    if (!open || !btn.current || !tip.current) return
    const b = btn.current.getBoundingClientRect()
    const t = tip.current.getBoundingClientRect()
    const vw = document.documentElement.clientWidth
    const left = Math.max(8, Math.min(b.left, vw - t.width - 8))
    const below = b.bottom + 6
    const top = below + t.height > window.innerHeight - 8 ? b.top - t.height - 6 : below
    setPos({ left, top: Math.max(8, top) })
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onScroll = () => setOpen(false)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open])

  const show = () => setOpen(true)
  const hide = () => {
    setOpen(false)
    setPos(null)
  }
  return (
    <span className="cite-wrap">
      <button
        ref={btn}
        type="button"
        className="cite"
        aria-label={`Source ${n}: ${citationText(source)}`}
        aria-describedby={open ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={() => {
          hide()
          onCite(n)
        }}
      >
        {n}
      </button>
      {open && (
        <span
          ref={tip}
          id={id}
          role="tooltip"
          className="cite-tip"
          style={pos ? { left: pos.left, top: pos.top } : { visibility: 'hidden' }}
        >
          <strong className="cite-tip-title">{citationText(source)}</strong>
          <span className="cite-tip-text">
            <Marked text={snippet(source.text, re, 220)} re={re} />
          </span>
          <span className="cite-tip-hint">Click to jump to the source</span>
        </span>
      )}
    </span>
  )
}

/** The answer text with `[n]` markers that point at a real source turned into citation buttons. */
export function AnswerText({
  text,
  sources,
  re,
  onCite,
}: {
  text: string
  sources: Source[]
  re: RegExp | null
  onCite: (n: number) => void
}) {
  return (
    <>
      {parseAnswer(text, sources.length).map((p, i) =>
        p.kind === 'text' ? (
          <span key={i}>{p.text}</span>
        ) : (
          <Cite key={i} n={p.n} source={sources[p.n - 1]!} re={re} onCite={onCite} />
        ),
      )}
    </>
  )
}
