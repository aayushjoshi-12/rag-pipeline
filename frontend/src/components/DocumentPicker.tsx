import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { Document } from '../types'
import { Icon } from './Icon'

/** Options rendered at once; the filter narrows the rest (keeps hundreds of documents fast). */
export const RENDER_CAP = 200
const VISIBLE_CHIPS = 2

const ALL = '__all__'

/**
 * "Search in" multi-select: a combobox (filter input) with a listbox of ready documents.
 * None selected = all documents. APG pattern: DOM focus stays on the input, the active option is
 * announced through aria-activedescendant, Enter toggles it, selection does not follow focus.
 */
export function DocumentPicker({
  documents,
  selected,
  onChange,
}: {
  documents: Document[]
  selected: string[]
  onChange: (ids: string[]) => void
}) {
  const uid = useId()
  const listId = `${uid}-list`
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [active, setActive] = useState(0)
  const [showAllChips, setShowAllChips] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLUListElement>(null)

  const ready = useMemo(() => documents.filter((d) => d.status === 'ready'), [documents])
  const notReady = documents.length - ready.length
  const byId = useMemo(() => new Map(ready.map((d) => [d.id, d])), [ready])
  const chosen = selected.filter((id) => byId.has(id))
  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return q ? ready.filter((d) => d.filename.toLowerCase().includes(q)) : ready
  }, [ready, filter])
  const shown = matches.slice(0, RENDER_CAP)
  // option 0 is "All documents" (only while not filtering); the rest are documents
  const showAll = filter.trim() === ''
  const options: string[] = [...(showAll ? [ALL] : []), ...shown.map((d) => d.id)]
  const optId = (key: string) => `${uid}-opt-${key}`
  const activeKey = options[Math.min(active, options.length - 1)]

  useEffect(() => {
    if (!open) return
    const out = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', out)
    return () => document.removeEventListener('pointerdown', out)
  }, [open])

  useEffect(() => {
    if (open && activeKey)
      document.getElementById(optId(activeKey))?.scrollIntoView?.({ block: 'nearest' })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- optId is derived from uid
  }, [open, activeKey])

  const toggle = (key: string) => {
    if (key === ALL) onChange([])
    else onChange(chosen.includes(key) ? chosen.filter((x) => x !== key) : [...chosen, key])
  }
  const move = (to: number) => {
    setOpen(true)
    setActive(Math.max(0, Math.min(options.length - 1, to)))
  }
  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        move(open ? active + 1 : active)
        break
      case 'ArrowUp':
        e.preventDefault()
        move(open ? active - 1 : active)
        break
      case 'Home':
        if (open) {
          e.preventDefault()
          move(0)
        }
        break
      case 'End':
        if (open) {
          e.preventDefault()
          move(options.length - 1)
        }
        break
      case 'Enter':
        if (open && activeKey) {
          e.preventDefault()
          toggle(activeKey)
        }
        break
      case 'Escape':
        if (open) {
          e.preventDefault()
          e.stopPropagation()
          setOpen(false)
        } else if (filter) setFilter('')
        break
      case 'Backspace':
        if (filter === '' && chosen.length > 0) onChange(chosen.slice(0, -1))
        break
    }
  }

  const chips = showAllChips ? chosen : chosen.slice(0, VISIBLE_CHIPS)
  const hidden = chosen.length - chips.length
  const label = chosen.length === 0 ? 'All documents' : `${chosen.length} selected`

  return (
    <div className="picker" ref={root}>
      <div className="picker-field" onClick={() => input.current?.focus()}>
        <span className="picker-label" id={`${uid}-label`}>
          Search in
        </span>
        <Icon name="file" size={14} className="picker-icon" />
        {chosen.length === 0 && <span className="picker-all">All documents</span>}
        {chips.map((id) => (
          <span key={id} className="chip chip-sel">
            <span className="truncate" title={byId.get(id)?.filename}>
              {byId.get(id)?.filename}
            </span>
            <button
              type="button"
              className="chip-x"
              aria-label={`Remove ${byId.get(id)?.filename}`}
              onClick={(e) => {
                e.stopPropagation()
                onChange(chosen.filter((x) => x !== id))
              }}
            >
              <Icon name="close" size={12} />
            </button>
          </span>
        ))}
        {hidden > 0 && (
          <button
            type="button"
            className="chip chip-more"
            aria-label={`Show ${hidden} more selected documents`}
            onClick={(e) => {
              e.stopPropagation()
              setShowAllChips(true)
            }}
          >
            +{hidden}
          </button>
        )}
        {showAllChips && chosen.length > VISIBLE_CHIPS && (
          <button
            type="button"
            className="chip chip-more"
            onClick={(e) => {
              e.stopPropagation()
              setShowAllChips(false)
            }}
          >
            Show fewer
          </button>
        )}
        <input
          ref={input}
          id={`${uid}-input`}
          className="picker-input"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-labelledby={`${uid}-label`}
          aria-describedby={`${uid}-status`}
          aria-activedescendant={open && activeKey ? optId(activeKey) : undefined}
          autoComplete="off"
          spellCheck={false}
          placeholder={ready.length ? 'Filter documents…' : 'No ready documents'}
          value={filter}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setFilter(e.target.value)
            setActive(0)
            setOpen(true)
          }}
          onKeyDown={onKeyDown}
        />
        {chosen.length > 0 && (
          <span className="picker-badge num" aria-hidden="true">
            {chosen.length}
          </span>
        )}
        <button
          type="button"
          className="picker-toggle"
          aria-label={open ? 'Close document list' : 'Open document list'}
          aria-expanded={open}
          aria-controls={listId}
          tabIndex={-1}
          onClick={(e) => {
            e.stopPropagation()
            setOpen((v) => !v)
            input.current?.focus()
          }}
        >
          <Icon name="chevron" size={14} className="picker-chev" />
        </button>
      </div>
      <span id={`${uid}-status`} className="sr-only" role="status">
        {label}
      </span>
      {open && (
        <div className="picker-pop">
          <ul
            ref={list}
            id={listId}
            role="listbox"
            aria-multiselectable="true"
            aria-labelledby={`${uid}-label`}
            className="picker-list"
          >
            {showAll && (
              <li
                id={optId(ALL)}
                role="option"
                aria-selected={chosen.length === 0}
                className={`picker-opt${activeKey === ALL ? ' is-active' : ''}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => toggle(ALL)}
              >
                <span className="picker-check" aria-hidden="true">
                  {chosen.length === 0 && <Icon name="check" size={12} />}
                </span>
                All documents
                <span className="muted small num picker-n">{ready.length}</span>
              </li>
            )}
            {shown.map((d) => (
              <li
                key={d.id}
                id={optId(d.id)}
                role="option"
                aria-selected={chosen.includes(d.id)}
                className={`picker-opt${activeKey === d.id ? ' is-active' : ''}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => toggle(d.id)}
              >
                <span className="picker-check" aria-hidden="true">
                  {chosen.includes(d.id) && <Icon name="check" size={12} />}
                </span>
                <span className="truncate" title={d.filename}>
                  {d.filename}
                </span>
              </li>
            ))}
            {matches.length === 0 && (
              <li className="picker-empty" role="presentation">
                No ready document matches &ldquo;{filter}&rdquo;.
              </li>
            )}
          </ul>
          <p className="picker-foot muted small">
            {matches.length > RENDER_CAP
              ? `Showing ${RENDER_CAP} of ${matches.length}. Type to narrow the list. `
              : ''}
            {notReady > 0
              ? `${notReady} ${notReady === 1 ? 'document is' : 'documents are'} not ready yet and cannot be searched.`
              : ''}
            {matches.length <= RENDER_CAP && notReady === 0
              ? 'Up and down to move, Enter to select, Esc to close.'
              : ''}
          </p>
        </div>
      )}
    </div>
  )
}
