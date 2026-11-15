import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  FUSION_HELP,
  MODE_HELP,
  NEIGHBORS_MAX,
  TOP_K_MAX,
  intOrEmpty,
  overrideParts,
  prefsSummary,
  type RetrievalPrefs,
} from '../retrieval'
import { useCan } from '../store/auth'
import type { RetrievalFusion, RetrievalMode } from '../types'
import { Icon } from './Icon'

/**
 * Per-request retrieval overrides in a compact "Advanced" popover. Defaults live in Admin > Settings;
 * anything set here applies to this browser's questions only, and is flagged next to the button.
 */
export function RetrievalOptions({
  value,
  onChange,
}: {
  value: RetrievalPrefs
  onChange: (next: RetrievalPrefs) => void
}) {
  const [open, setOpen] = useState(false)
  const panel = useId()
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const canRead = useCan('settings:read')
  const canManage = useCan('settings:manage')
  const canSettings = canRead || canManage
  const hybrid = value.mode === 'hybrid'
  const active = overrideParts(value).length > 0

  useEffect(() => {
    if (!open) return
    const out = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        trigger.current?.focus()
      }
    }
    document.addEventListener('pointerdown', out)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('pointerdown', out)
      document.removeEventListener('keydown', key)
    }
  }, [open])

  return (
    <div className="advanced" ref={root}>
      <button
        ref={trigger}
        type="button"
        className={`btn btn-secondary btn-sm advanced-btn${active ? ' is-active' : ''}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? panel : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="settings" size={14} />
        Advanced
        {active && <span className="advanced-dot" aria-hidden="true" />}
      </button>
      {active && (
        <span className="chip chip-static chip-override" data-testid="retrieval-summary">
          <span className="sr-only">Overriding server defaults: </span>
          {prefsSummary(value)}
        </span>
      )}
      {open && (
        <div id={panel} role="dialog" aria-label="Retrieval options" className="advanced-pop">
          <div className="field">
            <label className="field-label" htmlFor={`${panel}-mode`}>
              Retrieval mode
            </label>
            <select
              id={`${panel}-mode`}
              className="select"
              value={value.mode}
              aria-describedby={`${panel}-mode-help`}
              onChange={(e) => onChange({ ...value, mode: e.target.value as '' | RetrievalMode })}
            >
              <option value="">Server default</option>
              <option value="semantic">Semantic</option>
              <option value="keyword">Keyword</option>
              <option value="hybrid">Hybrid</option>
            </select>
            <p id={`${panel}-mode-help`} className="field-help">
              {MODE_HELP[value.mode]}
            </p>
          </div>
          {hybrid && (
            <div className="field">
              <label className="field-label" htmlFor={`${panel}-fusion`}>
                Fusion
              </label>
              <select
                id={`${panel}-fusion`}
                className="select"
                value={value.fusion}
                aria-describedby={`${panel}-fusion-help`}
                onChange={(e) =>
                  onChange({ ...value, fusion: e.target.value as '' | RetrievalFusion })
                }
              >
                <option value="">Server default</option>
                <option value="rrf">RRF</option>
                <option value="weighted">Weighted</option>
              </select>
              <p id={`${panel}-fusion-help`} className="field-help">
                {FUSION_HELP[value.fusion]}
              </p>
            </div>
          )}
          <div className="advanced-pair">
            <div className="field">
              <label className="field-label" htmlFor={`${panel}-topk`}>
                Passages (top_k)
              </label>
              <input
                id={`${panel}-topk`}
                className="input"
                type="number"
                inputMode="numeric"
                min={1}
                max={TOP_K_MAX}
                placeholder="Default"
                value={value.topK ?? ''}
                onChange={(e) =>
                  onChange({ ...value, topK: intOrEmpty(e.target.value, 1, TOP_K_MAX) })
                }
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${panel}-neighbors`}>
                Neighbours
              </label>
              <select
                id={`${panel}-neighbors`}
                className="select"
                value={value.neighbors ?? ''}
                onChange={(e) => onChange({ ...value, neighbors: e.target.value })}
              >
                <option value="">Default</option>
                {Array.from({ length: NEIGHBORS_MAX + 1 }, (_, i) => (
                  <option key={i} value={String(i)}>
                    {i}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="advanced-foot">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={!active}
              onClick={() => onChange({ mode: '', fusion: '', topK: '', neighbors: '' })}
            >
              Reset to defaults
            </button>
            <p className="field-help">
              Defaults are set in{' '}
              {canSettings ? <Link to="/settings">Admin &gt; Settings</Link> : 'Admin > Settings'}.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
