import { useEffect, useState } from 'react'
import { api } from '../api/client'
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
    <div>
      <h1>Users</h1>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <section>
        <h2>New user</h2>
        <form
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
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <fieldset>
            <legend>Roles</legend>
            {roles.map((r) => (
              <label key={r.id}>
                <input
                  type="checkbox"
                  checked={roleIds.includes(r.id)}
                  onChange={() =>
                    setRoleIds((cur) =>
                      cur.includes(r.id) ? cur.filter((x) => x !== r.id) : [...cur, r.id],
                    )
                  }
                />{' '}
                {r.name}
              </label>
            ))}
          </fieldset>
          <button type="submit">Create user</button>
        </form>
      </section>

      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Roles</th>
            <th>Active</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.email}</td>
              <td>
                {roles.length > 0
                  ? roles.map((r) => (
                      <label key={r.id}>
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
                        />{' '}
                        {r.name}{' '}
                      </label>
                    ))
                  : u.roles.join(', ')}
              </td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`${u.email} active`}
                  checked={u.is_active}
                  onChange={() => void act(() => api.updateUser(u.id, { is_active: !u.is_active }))}
                />
              </td>
              <td>
                {u.id !== myId && (
                  <button
                    aria-label={`delete ${u.email}`}
                    onClick={() => {
                      if (window.confirm(`Delete ${u.email}?`)) void act(() => api.deleteUser(u.id))
                    }}
                  >
                    Delete
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
