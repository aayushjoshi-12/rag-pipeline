import {
  createContext,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { citationText, highlight, snippet, splitCitations } from '../citations'
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

// Minimal structural types for the mdast tree (the plugin only needs these).
interface MdNode {
  type: string
  value?: string
  children?: MdNode[]
  data?: { hName: string; hProperties: Record<string, unknown> }
}
// Text inside these is never turned into markers: links keep `[1](url)` as a link.
const SKIP = new Set(['link', 'linkReference', 'code', 'inlineCode', 'definition'])

/** Remark plugin: `[n]` in text nodes (paragraphs, list items, emphasis, headings, cells) become
 * `cite-marker` elements. Code and links are never touched, so those stay plain text. */
function remarkCitations(count: number) {
  const walk = (node: MdNode) => {
    if (!node.children || SKIP.has(node.type)) return
    const out: MdNode[] = []
    for (const child of node.children) {
      if (child.type !== 'text') {
        walk(child)
        out.push(child)
        continue
      }
      for (const part of splitCitations(child.value ?? '', count)) {
        out.push(
          part.kind === 'text'
            ? { type: 'text', value: part.text }
            : {
                type: 'cite',
                data: { hName: 'cite-marker', hProperties: { n: part.n } },
              },
        )
      }
    }
    node.children = out
  }
  return (tree: MdNode) => walk(tree)
}

/** While streaming: close a dangling inline code / bold and hide a half-typed `[1` marker so the
 * structure does not flicker between chunks. Fenced blocks and lists parse fine unclosed. */
export function healStreaming(text: string): string {
  let out = text.replace(/\[\d{0,3}(?:\s*,\s*\d{0,3})*\s*$/, '')
  const fences = out.match(/^ {0,3}(```|~~~)/gm)?.length ?? 0
  if (fences % 2 === 1) return out // inside an open fence: leave it alone
  const lastLine = out.slice(out.lastIndexOf('\n') + 1)
  if ((lastLine.match(/`/g)?.length ?? 0) % 2 === 1) out += '`'
  else if ((lastLine.replace(/`[^`]*`/g, '').match(/\*\*/g)?.length ?? 0) % 2 === 1) out += '**'
  return out
}

interface CiteCtx {
  sources: Source[]
  re: RegExp | null
  onCite: (n: number) => void
}
const CiteContext = createContext<CiteCtx>({ sources: [], re: null, onCite: () => {} })

function CiteMarker({ n }: CiteProps) {
  const { sources, re, onCite } = useContext(CiteContext)
  const source = n ? sources[n - 1] : undefined
  return source && n ? <Cite n={n} source={source} re={re} onCite={onCite} /> : null
}

type CiteProps = { n?: number } & ExtraProps

/** The answer text rendered as safe markdown (no raw HTML) with `[n]` markers that point at a real
 * source turned into citation buttons. */
export function AnswerText({
  text,
  sources,
  re,
  onCite,
  streaming = false,
}: {
  text: string
  sources: Source[]
  re: RegExp | null
  onCite: (n: number) => void
  streaming?: boolean
}) {
  // `components` must keep a stable identity or every render would remount the markers (and close
  // their popovers); the live values reach them through a context.
  const components = useMemo(
    () =>
      ({
        'cite-marker': CiteMarker,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        ),
        table: ({ children }) => (
          <div className="md-table" tabIndex={0} role="region" aria-label="Table">
            <table>{children}</table>
          </div>
        ),
        h1: ({ children }) => <h4 className="md-h">{children}</h4>,
        h2: ({ children }) => <h4 className="md-h">{children}</h4>,
        h3: ({ children }) => <h4 className="md-h">{children}</h4>,
        h4: ({ children }) => <h4 className="md-h">{children}</h4>,
        h5: ({ children }) => <h4 className="md-h">{children}</h4>,
        h6: ({ children }) => <h4 className="md-h">{children}</h4>,
        pre: ({ children }) => (
          <pre tabIndex={0} className="md-pre">
            {children}
          </pre>
        ),
      }) as Components,
    [],
  )
  const plugins = useMemo(
    () => [remarkGfm, () => remarkCitations(sources.length)],
    [sources.length],
  )
  const ctx = useMemo(() => ({ sources, re, onCite }), [sources, re, onCite])
  return (
    <CiteContext.Provider value={ctx}>
      <ReactMarkdown
        remarkPlugins={plugins}
        components={components}
        skipHtml
        disallowedElements={['img']}
        unwrapDisallowed
      >
        {streaming ? healStreaming(text) : text}
      </ReactMarkdown>
    </CiteContext.Provider>
  )
}
