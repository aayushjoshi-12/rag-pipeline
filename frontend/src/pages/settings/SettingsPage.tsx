import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import { Icon } from '../../components/Icon'
import { Alert, Card, Dialog, EmptyState, PageHeader, TabPanel, Tabs } from '../../components/ui'
import { POLL_MS } from '../../config'
import { useCan } from '../../store/auth'
import type {
  ConnectionTest,
  ReindexStatus,
  SettingItem,
  SettingValue,
  SettingsPatchInput,
  SettingsView,
} from '../../types'
import { AuditTab } from './AuditTab'
import { ALL_FLOW_KEYS, FLOWS, type Flow } from './flows'
import { SettingField, shown, type Pending } from './SettingField'
import { msg } from '../../lib'

/** Per-key validation messages out of a 422 `detail` list (`loc: ["settings", key]`). */
function fieldErrors(e: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  if (!(e instanceof ApiError)) return out
  for (const d of Array.isArray(e.detail) ? (e.detail as unknown[]) : []) {
    if (d && typeof d === 'object' && 'loc' in d && 'msg' in d) {
      const loc = (d as { loc: unknown }).loc
      const key = Array.isArray(loc) ? String(loc[loc.length - 1]) : ''
      if (key) out[key] = String((d as { msg: unknown }).msg)
    }
  }
  // 409 detail objects carry `errors: {key: reason}`
  const detail: unknown = e.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail) && 'errors' in detail) {
    Object.assign(out, (detail as { errors: Record<string, string> }).errors)
  }
  return out
}

/** A copy of `obj` without `key`. */
function without<T>(obj: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key))
}

const GROUP_DOCS: Record<string, string> = {
  Retrieval: 'docs/configuration.md#retrieval',
  Cache: 'docs/configuration.md#query-cache',
  Ingestion: 'docs/adr/0003-chunking.md',
  Parsing: 'docs/adr/0002-document-parsing.md',
  Storage: 'docs/configuration.md#storage',
}

function ReindexPanel({
  status,
  canManage,
  onQueue,
  busy,
}: {
  status: ReindexStatus
  canManage: boolean
  onQueue: () => void
  busy: boolean
}) {
  const active = status.state === 'pending' || status.state === 'running'
  if (!status.needed && status.state === 'idle') return null
  return (
    <Card
      title="Reindex"
      description="The vector index is rebuilt from the chunks already stored; documents are not re-parsed."
      className="reindex-panel"
    >
      <div role="status" aria-live="polite" data-testid="reindex-status">
        <p>
          <span
            className={`badge badge-${status.state === 'done' ? 'done' : status.state === 'failed' ? 'failed' : active ? 'running' : 'pending'}`}
          >
            {status.state}
          </span>{' '}
          {status.queued_at ? `queued by ${status.queued_by ?? 'unknown'}` : 'not queued yet'}
          {status.kind ? ` (${status.kind})` : ''}
        </p>
        {status.state === 'done' && status.result && (
          <p className="small muted">
            {String(status.result.documents ?? '?')} documents,{' '}
            {String(status.result.vectors ?? '?')} vectors
          </p>
        )}
        {status.error && <p className="small setting-error">Task failed: {status.error}</p>}
        {status.problems.length > 0 && (
          <>
            <p className="small">
              The stored index does not match the settings. Search may return errors until a reindex
              finishes:
            </p>
            <ul className="small">
              {status.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      {canManage && status.needed && !active && (
        <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={onQueue}>
          Queue reindex
        </button>
      )}
    </Card>
  )
}

export default function SettingsPage() {
  const canManage = useCan('settings:manage')
  const [tab, setTab] = useState('settings')
  const [view, setView] = useState<SettingsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<Record<string, Pending>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [query, setQuery] = useState('')
  const [saving, setSaving] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [tests, setTests] = useState<Record<string, ConnectionTest | 'running'>>({})
  const [reindex, setReindex] = useState<ReindexStatus | null>(null)
  const [reindexBusy, setReindexBusy] = useState(false)
  /** Bumped after save/discard so field-local state (an open secret box) resets. */
  const [gen, setGen] = useState(0)
  const alive = useRef(true)

  const load = useCallback(async () => {
    try {
      setView(await api.getSettings())
      setError(null)
    } catch (e) {
      if (alive.current) setError(msg(e))
    }
  }, [])

  const loadReindex = useCallback(async () => {
    try {
      const s = await api.reindexStatus()
      if (alive.current) setReindex(s)
      return s
    } catch {
      return null
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    void loadReindex()
    return () => {
      alive.current = false
    }
  }, [load, loadReindex])

  // follow a running reindex
  useEffect(() => {
    if (!reindex || (reindex.state !== 'pending' && reindex.state !== 'running')) return
    const t = setInterval(() => void loadReindex(), POLL_MS * 2)
    return () => clearInterval(t)
  }, [reindex, loadReindex])

  const items = useMemo(() => {
    const m = new Map<string, SettingItem>()
    for (const s of view?.settings ?? []) m.set(s.key, s)
    return m
  }, [view])

  const effective = (key: string): SettingValue => {
    const p = pending[key]
    if (p?.kind === 'set') return p.value
    if (p?.kind === 'reset') return items.get(key)?.default ?? null
    return items.get(key)?.value ?? null
  }

  const setPendingFor = (item: SettingItem, value: SettingValue) => {
    setNotice(null)
    setErrors((e) => {
      return without(e, item.key)
    })
    setPending((p) => {
      // setting a field back to what it already is drops the pending change
      if (!item.secret && value === item.value) {
        return without(p, item.key)
      }
      return { ...p, [item.key]: { kind: 'set', value } }
    })
  }
  const undo = (key: string) =>
    setPending((p) => {
      return without(p, key)
    })
  const queueReset = (key: string) => {
    setNotice(null)
    setPending((p) => ({ ...p, [key]: { kind: 'reset' } }))
  }

  const pendingKeys = Object.keys(pending)
  const reindexKeys = pendingKeys.filter((k) => items.get(k)?.apply === 'reindex')

  const body = (): SettingsPatchInput => {
    const set: Record<string, SettingValue> = {}
    const reset: string[] = []
    for (const [k, p] of Object.entries(pending)) {
      if (p.kind === 'set') set[k] = p.value
      else reset.push(k)
    }
    return { set, reset }
  }

  const save = async (confirmReindex: boolean) => {
    setSaving(true)
    setErrors({})
    setError(null)
    try {
      const out = await api.patchSettings({ ...body(), confirm_reindex: confirmReindex })
      setPending({})
      setGen((g) => g + 1)
      setConfirming(false)
      const parts = [
        `Saved ${out.changed.length + out.reset.length} setting(s); live settings are already in use.`,
      ]
      if (out.restart_required.length)
        parts.push(`Restart needed for: ${out.restart_required.join(', ')}.`)
      if (out.reindex) {
        parts.push(
          out.reindex.queued
            ? 'A reindex was queued; follow it below.'
            : `Saved, but the reindex could not be queued: ${out.reindex.error ?? 'unknown error'}`,
        )
      }
      setNotice(parts.join(' '))
      await load()
      await loadReindex()
    } catch (e) {
      setConfirming(false)
      const fe = fieldErrors(e)
      if (Object.keys(fe).length) setErrors(fe)
      setError(msg(e))
    } finally {
      setSaving(false)
    }
  }

  const onSave = () => {
    if (reindexKeys.length) setConfirming(true)
    else void save(false)
  }

  const runTest = async (flow: Flow) => {
    if (!flow.service) return
    setTests((t) => ({ ...t, [flow.id]: 'running' }))
    try {
      const out = await api.testConnection(flow.service)
      setTests((t) => ({ ...t, [flow.id]: out }))
    } catch (e) {
      setTests((t) => ({
        ...t,
        [flow.id]: {
          service: flow.service ?? '',
          ok: false,
          detail: msg(e),
          provider: '',
          latency_ms: 0,
        },
      }))
    }
  }

  const queueReindex = async () => {
    setReindexBusy(true)
    try {
      await api.queueReindex(reindex?.kind === 'rebuild_fts' ? 'rebuild_fts' : 'reindex')
      await loadReindex()
    } catch (e) {
      setError(msg(e))
    } finally {
      setReindexBusy(false)
    }
  }

  const field = (key: string) => {
    const item = items.get(key)
    if (!item) return null
    return (
      <SettingField
        key={`${key}:${gen}`}
        item={item}
        pending={pending[key]}
        error={errors[key]}
        canManage={canManage}
        onSet={(v) => setPendingFor(item, v)}
        onReset={() => queueReset(key)}
        onUndo={() => undo(key)}
      />
    )
  }

  const q = query.trim().toLowerCase()
  const match = (s: SettingItem) =>
    !q ||
    s.key.includes(q) ||
    s.description.toLowerCase().includes(q) ||
    s.group.toLowerCase().includes(q)
  const flowMatches = (flow: Flow) =>
    !q ||
    [
      flow.title,
      flow.description,
      ...flow.keys(effective).map((k) => items.get(k)?.description ?? ''),
    ].some((t) => t.toLowerCase().includes(q)) ||
    flow.keys(effective).some((k) => k.includes(q))

  const rest = (view?.settings ?? []).filter((s) => !ALL_FLOW_KEYS.has(s.key) && match(s))
  const groups = (view?.groups ?? [])
    .map((g) => ({ g, rows: rest.filter((s) => s.group === g.id) }))
    .filter((x) => x.rows.length > 0)
  const summary = view?.summary

  const tabs = [
    { id: 'settings', label: 'Settings' },
    ...(canManage ? [{ id: 'audit', label: 'Audit log' }] : []),
  ]

  return (
    <div className="stack settings-page">
      <PageHeader
        title="Settings"
        description="Defaults and services for this deployment. A value set by an environment variable wins and is shown locked; otherwise a value set here wins over the config file and the built-in default."
      />
      {error && <Alert>{error}</Alert>}
      {summary?.overrides_error && (
        <Alert tone="warning">
          The stored settings are invalid and are not applied (the last good values stay in force):{' '}
          {summary.overrides_error}
        </Alert>
      )}
      {summary && summary.restart_pending.length > 0 && (
        <Alert tone="warning">
          Restart needed for: <strong>{summary.restart_pending.join(', ')}</strong>. The new values
          are saved and take effect when the API restarts.
        </Alert>
      )}
      {summary && !summary.secrets_enabled && (
        <Alert tone="warning">
          <strong>Secrets are disabled:</strong> the server has no{' '}
          <code>SETTINGS_ENCRYPTION_KEY</code>, so API keys cannot be stored here. Set it (a random
          value of 32+ characters) in the server environment and restart; everything else on this
          page works.
        </Alert>
      )}
      {notice && (
        <p role="status" className="notice" data-testid="notice">
          <Icon name="checkCircle" size={16} /> {notice}
        </p>
      )}

      <Tabs tabs={tabs} value={tab} onChange={setTab} label="Settings sections" />

      {tab === 'settings' && (
        <TabPanel id="settings">
          {!view && !error && <p className="muted">Loading…</p>}
          {summary && summary.missing.length > 0 && (
            <Card
              title="Finish setting up"
              description="Nothing here stops the app starting, but these features will not work yet."
              className="first-run"
            >
              <ul className="missing-list">
                {summary.missing.map((m) => (
                  <li key={m.area}>
                    <Icon name="alert" size={16} /> {m.message}
                  </li>
                ))}
              </ul>
              <p className="small muted">
                Use the matching section below to add the key, then press Test connection.
              </p>
            </Card>
          )}

          <div className="settings-search">
            <label className="field">
              <span className="field-label">Search settings</span>
              <input
                className="input"
                type="search"
                placeholder="e.g. top_k, cache, chunk"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          </div>

          {view && (
            <>
              <h2 className="section-title">Services</h2>
              {FLOWS.filter(flowMatches).map((flow) => {
                const result = tests[flow.id]
                const keys = flow.keys(effective)
                const sel = items.get(flow.selector)
                const provider = effective(flow.selector)
                return (
                  <Card
                    key={flow.id}
                    title={flow.title}
                    description={flow.description}
                    actions={
                      flow.service && canManage ? (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          disabled={result === 'running'}
                          onClick={() => void runTest(flow)}
                          aria-label={`Test connection: ${flow.title}`}
                        >
                          {result === 'running' ? 'Testing…' : 'Test connection'}
                        </button>
                      ) : undefined
                    }
                    className="flow"
                    aria-label={flow.title}
                  >
                    <p className="small flow-consequence">
                      Now: <strong>{shown(provider)}</strong>
                      {sel?.source ? ` (${sel.source})` : ''}. {flow.consequence}{' '}
                      <span className="muted">{flow.docs}</span>
                    </p>
                    {result && result !== 'running' && (
                      <p
                        role="status"
                        className={result.ok ? 'test-result test-ok' : 'test-result test-fail'}
                        data-testid={`test-${flow.id}`}
                      >
                        <Icon name={result.ok ? 'checkCircle' : 'xCircle'} size={16} />{' '}
                        {result.ok ? 'Works' : 'Failed'}: {result.detail}
                        {result.latency_ms ? ` (${result.latency_ms} ms)` : ''}
                      </p>
                    )}
                    {flow.service && canManage && (
                      <p className="small muted">
                        Tests the saved settings: save your changes first.
                      </p>
                    )}
                    <div className="settings-list">{keys.map((k) => field(k))}</div>
                  </Card>
                )
              })}

              <h2 className="section-title">Tunables and other settings</h2>
              {groups.length === 0 && (
                <EmptyState icon="inbox" title="No settings match">
                  Try a different search.
                </EmptyState>
              )}
              {groups.map(({ g, rows }) => (
                <Card
                  key={g.id}
                  title={g.id}
                  description={
                    <>
                      {g.description}
                      {GROUP_DOCS[g.id] ? <span className="muted"> {GROUP_DOCS[g.id]}</span> : null}
                    </>
                  }
                >
                  <div className="settings-list">{rows.map((s) => field(s.key))}</div>
                </Card>
              ))}
            </>
          )}

          {reindex && (
            <ReindexPanel
              status={reindex}
              canManage={canManage}
              onQueue={() => void queueReindex()}
              busy={reindexBusy}
            />
          )}
        </TabPanel>
      )}

      {tab === 'audit' && canManage && (
        <TabPanel id="audit">
          <AuditTab />
        </TabPanel>
      )}

      {pendingKeys.length > 0 && (
        <div className="pending-bar" role="region" aria-label="Pending changes">
          <span data-testid="pending-count">
            <strong>{pendingKeys.length}</strong> pending change
            {pendingKeys.length === 1 ? '' : 's'}
            {reindexKeys.length > 0 && ' (reindex needed)'}
          </span>
          <span className="pending-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setPending({})
                setGen((g) => g + 1)
              }}
              disabled={saving}
            >
              Discard
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={onSave}
              disabled={saving || !canManage}
            >
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </span>
        </div>
      )}

      {confirming && (
        <Dialog
          title="Confirm reindex"
          onEscape={() => setConfirming(false)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={saving}
                onClick={() => void save(true)}
              >
                Save and reindex
              </button>
            </>
          }
        >
          <Alert tone="warning">
            These changes alter how vectors are stored, so the whole index is rebuilt from the
            stored chunks. Search is unavailable or partial while it runs.
          </Alert>
          <ul>
            {reindexKeys.map((k) => (
              <li key={k}>
                <code>{k}</code>
                {items.get(k)?.note ? `: ${items.get(k)?.note}` : ''}
              </li>
            ))}
          </ul>
        </Dialog>
      )}
    </div>
  )
}
