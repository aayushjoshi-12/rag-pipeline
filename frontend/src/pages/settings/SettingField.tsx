import { useState } from 'react'
import { Icon } from '../../components/Icon'
import type { SettingItem, SettingValue } from '../../types'

export type Pending = { kind: 'set'; value: SettingValue } | { kind: 'reset' }

export function shown(v: SettingValue): string {
  if (v === null) return 'not set'
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  return String(v)
}

const SOURCE_LABEL: Record<SettingItem['source'], string> = {
  env: 'environment',
  ui: 'set here',
  config: 'config file',
  default: 'default',
}

function SourceBadge({ item }: { item: SettingItem }) {
  return (
    <span className={`badge src src-${item.source}`} data-testid={`source-${item.key}`}>
      {SOURCE_LABEL[item.source]}
    </span>
  )
}

const APPLY_LABEL: Record<SettingItem['apply'], string> = {
  live: 'live',
  restart: 'needs restart',
  reindex: 'needs reindex',
  bootstrap: 'read-only',
}

function parse(item: SettingItem, raw: string): SettingValue {
  if (raw.trim() === '' && item.nullable) return null
  if (item.kind === 'integer') return /^-?\d+$/.test(raw.trim()) ? Number(raw.trim()) : raw
  if (item.kind === 'number') {
    const n = Number(raw)
    return raw.trim() !== '' && Number.isFinite(n) ? n : raw
  }
  return raw
}

function when(value: string | null): string {
  if (!value) return ''
  const iso = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function SettingField({
  item,
  pending,
  error,
  canManage,
  onSet,
  onReset,
  onUndo,
}: {
  item: SettingItem
  pending: Pending | undefined
  error: string | undefined
  canManage: boolean
  onSet: (value: SettingValue) => void
  onReset: () => void
  onUndo: () => void
}) {
  const id = `setting-${item.key}`
  const helpId = `${id}-help`
  const errId = `${id}-err`
  const [editingSecret, setEditingSecret] = useState(false)
  const disabled = item.locked || !canManage || item.disabled_reason !== null
  const current: SettingValue = pending?.kind === 'set' ? pending.value : item.value
  const text = current === null ? '' : String(current)
  const resetting = pending?.kind === 'reset'
  const describedBy = [helpId, error ? errId : null].filter(Boolean).join(' ')

  let control
  if (item.secret) {
    const isSetting = pending?.kind === 'set' || editingSecret
    control = isSetting ? (
      <div className="secret-edit">
        <input
          id={id}
          className="input"
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          placeholder="Paste the new value"
          aria-describedby={describedBy}
          aria-invalid={error ? true : undefined}
          value={pending?.kind === 'set' ? String(pending.value ?? '') : ''}
          onChange={(e) => onSet(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => {
            setEditingSecret(false)
            onUndo()
          }}
        >
          Cancel
        </button>
      </div>
    ) : (
      <div className="secret-state">
        <span
          className={item.is_set ? 'badge badge-ok' : 'badge'}
          data-testid={`secret-${item.key}`}
        >
          {resetting ? 'will be cleared' : item.is_set ? 'set' : 'not set'}
        </span>
        {!disabled && (
          <>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              id={id}
              aria-label={`${item.is_set ? 'Replace' : 'Set'} ${item.key}`}
              aria-describedby={describedBy}
              onClick={() => setEditingSecret(true)}
            >
              {item.is_set ? 'Replace' : 'Set'}
            </button>
            {item.is_set && !resetting && (
              <button
                type="button"
                className="btn btn-ghost btn-danger-text btn-sm"
                aria-label={`Clear ${item.key}`}
                onClick={onReset}
              >
                Clear
              </button>
            )}
            {resetting && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={onUndo}>
                Undo
              </button>
            )}
          </>
        )}
      </div>
    )
  } else if (item.kind === 'boolean') {
    control = (
      <label className="check">
        <input
          id={id}
          type="checkbox"
          disabled={disabled}
          aria-describedby={describedBy}
          checked={current === true}
          onChange={(e) => onSet(e.target.checked)}
        />
        <span>{current === true ? 'On' : 'Off'}</span>
      </label>
    )
  } else if (item.kind === 'enum') {
    control = (
      <select
        id={id}
        className="select"
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        value={current === null ? '' : String(current)}
        onChange={(e) => onSet(e.target.value === '' ? null : e.target.value)}
      >
        {item.nullable && <option value="">(store default)</option>}
        {(item.choices ?? []).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    )
  } else if (item.kind === 'text') {
    control = (
      <textarea
        id={id}
        className="input"
        rows={4}
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        value={text}
        onChange={(e) => onSet(e.target.value)}
      />
    )
  } else {
    const numeric = item.kind === 'integer' || item.kind === 'number'
    control = (
      <input
        id={id}
        className="input"
        type={numeric ? 'number' : 'text'}
        step={item.kind === 'number' ? 'any' : undefined}
        inputMode={numeric ? 'decimal' : undefined}
        spellCheck={false}
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        placeholder={item.nullable ? (item.default === null ? 'not set' : shown(item.default)) : ''}
        value={text}
        onChange={(e) => onSet(parse(item, e.target.value))}
      />
    )
  }

  return (
    <div
      className={`setting${item.locked ? ' is-locked' : ''}${pending ? ' is-pending' : ''}`}
      data-testid={`row-${item.key}`}
    >
      <div className="setting-head">
        <label className="setting-label" htmlFor={id}>
          <code>{item.key}</code>
        </label>
        <span className="setting-badges">
          <SourceBadge item={item} />
          <span className={`badge apply apply-${item.apply}`}>{APPLY_LABEL[item.apply]}</span>
          {item.locked && (
            <span className="badge badge-locked" title={item.locked_reason ?? undefined}>
              <Icon name="lock" size={12} />
              locked
            </span>
          )}
          {item.restart_pending && (
            <span className="badge badge-pending-restart">restart to apply</span>
          )}
        </span>
      </div>
      <p className="setting-desc" id={helpId}>
        {item.description}
        {item.unit ? ` (${item.unit})` : ''}
      </p>
      <div className="setting-control">{control}</div>
      {item.locked && item.locked_reason && (
        <p className="small setting-lock">
          <Icon name="lock" size={13} /> {item.locked_reason}
          {item.shadowed &&
            (item.secret
              ? '. A value stored here is ignored while the variable is set.'
              : `. A value stored here (${shown(item.shadowed_value)}) is ignored while the variable is set.`)}
        </p>
      )}
      {item.disabled_reason && !item.locked && (
        <p className="small setting-lock">
          <Icon name="alert" size={13} /> {item.disabled_reason}
        </p>
      )}
      {item.undecryptable && (
        <p className="small setting-error">
          A stored value exists but cannot be decrypted with the current encryption key; it is not
          applied.
        </p>
      )}
      {error && (
        <p className="small setting-error" id={errId} role="alert">
          {error}
        </p>
      )}
      <p className="small muted setting-meta">
        {!item.secret && (
          <>
            Default: <strong>{shown(item.default)}</strong>
            {' · '}
          </>
        )}
        {item.source === 'ui' && item.updated_at
          ? `changed by ${item.updated_by ?? 'unknown'}, ${when(item.updated_at)}`
          : `source: ${SOURCE_LABEL[item.source]}`}
        {item.note ? ` · ${item.note}` : ''}
      </p>
      {item.source === 'ui' && !item.locked && canManage && !resetting && !item.secret && (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          aria-label={`Reset ${item.key} to default`}
          onClick={onReset}
        >
          Reset to {item.default === null ? 'unset' : 'default'}
        </button>
      )}
      {resetting && !item.secret && (
        <p className="small">
          Will be reset.{' '}
          <button type="button" className="btn btn-ghost btn-sm" onClick={onUndo}>
            Undo
          </button>
        </p>
      )}
    </div>
  )
}
