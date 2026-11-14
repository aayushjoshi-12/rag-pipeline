import { useId, useState } from 'react'
import { FUSION_HELP, MODE_HELP, prefsSummary, type RetrievalPrefs } from '../retrieval'
import type { RetrievalFusion, RetrievalMode } from '../types'
import { Icon } from './Icon'

/** Compact "advanced" disclosure in the chat composer: retrieval mode and hybrid fusion. */
export function RetrievalOptions({
  value,
  onChange,
}: {
  value: RetrievalPrefs
  onChange: (next: RetrievalPrefs) => void
}) {
  const [open, setOpen] = useState(false)
  const panel = useId()
  const hybrid = value.mode === 'hybrid'
  return (
    <div className="retrieval">
      <button
        type="button"
        className="linklike small retrieval-toggle"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="chevron" size={13} className="retrieval-chevron" />
        Retrieval options
        <span className="chip chip-static" data-testid="retrieval-summary">
          {prefsSummary(value)}
        </span>
      </button>
      {open && (
        <div id={panel} className="retrieval-panel">
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
          <div className="field">
            <label className="field-label" htmlFor={`${panel}-fusion`}>
              Fusion
            </label>
            <select
              id={`${panel}-fusion`}
              className="select"
              value={value.fusion}
              disabled={!hybrid}
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
              {hybrid ? FUSION_HELP[value.fusion] : 'Only used by hybrid mode.'}
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
