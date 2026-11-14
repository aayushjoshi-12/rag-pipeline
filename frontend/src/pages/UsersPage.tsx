import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { Alert, Card, PageHeader } from '../components/ui'
import { useAuth } from '../store/auth'
import type { ManagedUser, Role } from '../types'

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export default function UsersPage() {
  const myId = useAuth((s) => s.me?.id)
  const canSeeRoles = useAuth((s) => s.me?.permissions.includes('roles:manage') ?? false)
  const [users, setUsers] = useState<ManagedUser[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [error, setError] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [roleIds, setRoleIds] = useState<string[]>([])

  const [tick, setTick] = useState(0)
  useEffect(() => {
    let live = true
    // The role picker needs roles:manage; without it users can still be listed/deactivated.
    Promise.all([api.listUsers(), canSeeRoles ? api.listRoles() : Promise.resolve([] as Role[])])
      .then(([u, r]) => {
        if (live) {
          setUsers(u)
          setRoles(r)
        }
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [canSeeRoles, tick])

  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      setTick((n) => n + 1)
    } catch (e) {
      setError(msg(e))
    }
  }

  const roleId = (name: string): string | undefined => roles.find((r) => r.name === name)?.id

  return (
    <div className="stack">
      <PageHeader
        title="Users"
        description="Create accounts, assign roles and deactivate access."
      />
      {error && <Alert>{error}</Alert>}

      <Card title="New user" description="The user can sign in immediately with these credentials.">
        <form
          className="form form-grid"
          onSubmit={(e) => {
            e.preventDefault()
            void act(async () => {
              await api.createUser({ email, password, role_ids: roleIds })
              setEmail('')
              setPassword('')
              setRoleIds([])
            })
          }}
        >
          <label className="field">
            <span className="field-label">Email</span>
            <input
              className="input"
              type="email"
              autoComplete="off"
              spellCheck={false}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="field">
            <span className="field-label">Password</span>
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <fieldset className="fieldset span-all">
            <legend>Roles</legend>
            <div className="checks">
              {roles.map((r) => (
                <label key={r.id} className="check">
                  <input
                    type="checkbox"
                    checked={roleIds.includes(r.id)}
                    onChange={() =>
                      setRoleIds((cur) =>
                        cur.includes(r.id) ? cur.filter((x) => x !== r.id) : [...cur, r.id],
                      )
                    }
                  />
                  <span>{r.name}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="form-actions span-all">
            <button type="submit" className="btn btn-primary">
              Create user
            </button>
          </div>
        </form>
      </Card>

      <Card title="Accounts" description={`${users.length} accounts`} flush>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Roles</th>
                <th>Active</th>
                <th className="col-actions">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="cell-main">
                    <span className="truncate" title={u.email}>
                      {u.email}
                    </span>
                  </td>
                  <td>
                    <div className="checks">
                      {roles.length > 0
                        ? roles.map((r) => (
                            <label key={r.id} className="check">
                              <input
                                type="checkbox"
                                aria-label={`${u.email} ${r.name}`}
                                checked={u.roles.includes(r.name)}
                                onChange={() => {
                                  const names = u.roles.includes(r.name)
                                    ? u.roles.filter((n) => n !== r.name)
                                    : [...u.roles, r.name]
                                  void act(() =>
                                    api.updateUser(u.id, {
                                      role_ids: names.map(roleId).filter((x): x is string => !!x),
                                    }),
                                  )
                                }}
                              />
                              <span>{r.name}</span>
                            </label>
                          ))
                        : u.roles.map((n) => (
                            <span key={n} className="tag">
                              {n}
                            </span>
                          ))}
                    </div>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      className="switch"
                      aria-label={`${u.email} active`}
                      checked={u.is_active}
                      onChange={() =>
                        void act(() => api.updateUser(u.id, { is_active: !u.is_active }))
                      }
                    />
                  </td>
                  <td className="col-actions">
                    {u.id !== myId && (
                      <button
                        className="btn btn-ghost btn-danger-text btn-sm"
                        aria-label={`delete ${u.email}`}
                        onClick={() => {
                          if (window.confirm(`Delete ${u.email}?`))
                            void act(() => api.deleteUser(u.id))
                        }}
                      >
                        <Icon name="trash" size={15} />
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
