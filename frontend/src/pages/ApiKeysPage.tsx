import { useState } from 'react'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { Alert, Card, Dialog, EmptyState, PageHeader } from '../components/ui'
import { useLoadAct } from '../hooks'
import { msg, when } from '../lib'
import { useAuth } from '../store/auth'
import type { ApiKey, ApiKeyCreated, Role } from '../types'

const EXPIRY: { label: string; days: number | undefined }[] = [
  { label: 'Never', days: undefined },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
  { label: '180 days', days: 180 },
  { label: '1 year', days: 365 },
]

function KeyStatus({ status }: { status: ApiKey['status'] }) {
  const icon = status === 'active' ? 'checkCircle' : status === 'expired' ? 'clock' : 'xCircle'
  return (
    <span className={`badge badge-${status}`} data-testid="key-status">
      <Icon name={icon} size={13} />
      {status}
    </span>
  )
}

/** One-time reveal of a freshly created key. Closing it discards the secret for good. */
function RevealDialog({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const [copied, setCopied] = useState<'idle' | 'ok' | 'failed'>('idle')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(created.key)
      setCopied('ok')
    } catch {
      setCopied('failed')
    }
  }
  return (
    <Dialog
      title="Copy your API key"
      footer={
        <button type="button" className="btn btn-primary" onClick={onDone}>
          I have stored the key
        </button>
      }
    >
      <Alert tone="warning">
        <strong>You will not see this key again.</strong> Only a hash is stored, so it cannot be
        recovered. If you lose it, revoke it and create a new one.
      </Alert>
      {created.warnings.map((w) => (
        <Alert key={w} tone="warning">
          {w}
        </Alert>
      ))}
      <div>
        <span className="field-label">
          Key for <strong>{created.name}</strong> (role {created.role})
        </span>
        <div className="secret-box">
          <code className="secret-value" data-testid="new-key-value">
            {created.key}
          </code>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-label="Copy API key"
            onClick={() => void copy()}
          >
            <Icon name={copied === 'ok' ? 'check' : 'file'} size={15} />
            {copied === 'ok' ? 'Copied' : 'Copy'}
          </button>
        </div>
        <p className="small muted" role="status">
          {copied === 'ok' && 'Copied to the clipboard.'}
          {copied === 'failed' && 'Copy failed: select the key above and copy it manually.'}
        </p>
      </div>
    </Dialog>
  )
}

export default function ApiKeysPage() {
  const canSeeRoles = useAuth((s) => s.me?.permissions.includes('roles:manage') ?? false)
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [roles, setRoles] = useState<Role[]>([])
  const [name, setName] = useState('')
  const [roleId, setRoleId] = useState('')
  const [expiryIdx, setExpiryIdx] = useState(2) // 90 days: a safe default, "Never" is a deliberate choice
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)
  const [busy, setBusy] = useState(false)

  const { error, setError, act, reload } = useLoadAct(
    () =>
      Promise.all([
        api.listApiKeys(),
        canSeeRoles ? api.listRoles() : Promise.resolve([] as Role[]),
      ]),
    ([k, r]) => {
      setKeys(k)
      setRoles(r)
      setRoleId((cur) => cur || r.find((x) => x.name !== 'admin')?.id || '')
    },
    canSeeRoles,
  )

  const picked = roles.find((r) => r.id === roleId)

  const create = async () => {
    setError(null)
    setBusy(true)
    try {
      const out = await api.createApiKey({
        name: name.trim(),
        role_id: roleId,
        expires_in_days: EXPIRY[expiryIdx]?.days,
      })
      setCreated(out) // the only copy of the secret lives in this state until the dialog closes
      setName('')
      reload()
    } catch (e) {
      setError(msg(e))
    } finally {
      setBusy(false)
    }
  }

  const revoke = (k: ApiKey) => act(() => api.revokeApiKey(k.id))

  return (
    <div className="stack">
      <PageHeader
        title="API keys"
        description="Non-interactive credentials for services. Each key has the permissions of one role."
      />
      {error && <Alert>{error}</Alert>}

      <Card
        title="New API key"
        description="Send it as X-API-Key or as a Bearer token. The secret is shown once."
      >
        {!canSeeRoles ? (
          <Alert tone="warning">
            Choosing a role needs the roles:manage permission, which you do not hold.
          </Alert>
        ) : (
          <form
            className="form form-grid"
            onSubmit={(e) => {
              e.preventDefault()
              void create()
            }}
          >
            <label className="field">
              <span className="field-label">Name</span>
              <input
                className="input"
                autoComplete="off"
                spellCheck={false}
                maxLength={64}
                placeholder="billing-service"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </label>
            <div className="field">
              <label className="field-label" htmlFor="key-role">
                Role
              </label>
              <select
                id="key-role"
                className="select"
                aria-describedby="key-role-help"
                value={roleId}
                onChange={(e) => setRoleId(e.target.value)}
                required
              >
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
              <span className="field-help" id="key-role-help">
                {picked
                  ? picked.permissions.filter((p) => !p.endsWith(':manage')).join(', ') ||
                    'no permissions'
                  : ''}
              </span>
            </div>
            <label className="field">
              <span className="field-label">Expires</span>
              <select
                className="select"
                value={expiryIdx}
                onChange={(e) => setExpiryIdx(Number(e.target.value))}
              >
                {EXPIRY.map((x, i) => (
                  <option key={x.label} value={i}>
                    {x.label}
                  </option>
                ))}
              </select>
            </label>
            {picked?.name === 'admin' && (
              <div className="span-all">
                <Alert tone="warning">
                  The admin role lets this key read, write and delete everything. Prefer a narrow
                  role such as one with only search:use.
                </Alert>
              </div>
            )}
            <div className="form-actions span-all">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={busy || !name.trim() || !roleId}
              >
                Create key
              </button>
            </div>
          </form>
        )}
      </Card>

      <Card title="Keys" description={keys ? `${keys.length} keys` : undefined} flush>
        {keys && keys.length === 0 ? (
          <EmptyState icon="lock" title="No API keys yet">
            Create one above to let a service call the API without a user login.
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Key</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last used</th>
                  <th>Expires</th>
                  <th className="col-actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(keys ?? []).map((k) => (
                  <tr key={k.id}>
                    <td className="cell-main">
                      <span className="truncate" title={k.name}>
                        {k.name}
                      </span>
                    </td>
                    <td className="cell-mono">rag_{k.prefix}_…</td>
                    <td>
                      <span className="tag">{k.role}</span>
                    </td>
                    <td>
                      <KeyStatus status={k.status} />
                    </td>
                    <td>{when(k.last_used_at, 'never')}</td>
                    <td>{when(k.expires_at, 'no expiry')}</td>
                    <td className="col-actions">
                      {k.status === 'active' && (
                        <button
                          className="btn btn-ghost btn-danger-text btn-sm"
                          aria-label={`revoke ${k.name}`}
                          onClick={() => {
                            if (
                              window.confirm(
                                `Revoke "${k.name}"? Services using it will get 401 immediately.`,
                              )
                            )
                              void revoke(k)
                          }}
                        >
                          <Icon name="trash" size={15} />
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {created && <RevealDialog created={created} onDone={() => setCreated(null)} />}
    </div>
  )
}
