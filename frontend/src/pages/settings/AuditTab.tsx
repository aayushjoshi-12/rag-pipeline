import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { Alert, Card, EmptyState } from '../../components/ui'
import type { SettingValue, SettingsAuditRow } from '../../types'

const ACTIONS: Record<string, string> = {
  set: 'changed',
  reset: 'reset',
  set_secret: 'secret replaced',
  clear_secret: 'secret cleared',
  reindex_queued: 'reindex queued',
}

function when(value: string): string {
  const iso = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

const fmt = (v: SettingValue): string => (v === null ? 'unset' : String(v))

function change(r: SettingsAuditRow): string {
  if (r.secret) return '(secret value not recorded)'
  if (r.action === 'reindex_queued') return `task ${String(r.detail?.task_id ?? '')}`
  return `${fmt(r.old_value)} → ${fmt(r.new_value)}`
}

export function AuditTab() {
  const [rows, setRows] = useState<SettingsAuditRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [more, setMore] = useState(false)

  useEffect(() => {
    let live = true
    api
      .settingsAudit()
      .then((r) => {
        if (!live) return
        setRows(r)
        setMore(r.length >= 100)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      live = false
    }
  }, [])

  const older = async () => {
    const last = rows?.[rows.length - 1]
    if (!last) return
    try {
      const next = await api.settingsAudit(last.at)
      setRows([...(rows ?? []), ...next])
      setMore(next.length >= 100)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Card
      title="Audit log"
      description="Who changed which setting. Secret values are never recorded."
      flush
    >
      {error && <Alert>{error}</Alert>}
      {rows && rows.length === 0 ? (
        <EmptyState icon="inbox" title="No changes yet">
          Changes made on this page appear here.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table" data-testid="audit-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>What</th>
                <th>Setting</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {(rows ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="cell-nowrap">{when(r.at)}</td>
                  <td className="cell-wrap">{r.actor}</td>
                  <td>
                    <span className="tag">{ACTIONS[r.action] ?? r.action}</span>
                  </td>
                  <td className="cell-mono">{r.key}</td>
                  <td className="cell-wrap cell-change">{change(r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {more && (
        <div className="card-pad">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void older()}>
            Load older
          </button>
        </div>
      )}
    </Card>
  )
}
