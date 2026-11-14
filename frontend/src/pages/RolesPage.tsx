import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PermissionInfo, Role } from '../types'

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export default function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([])
  const [catalogue, setCatalogue] = useState<PermissionInfo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [picked, setPicked] = useState<string[]>([])

  const [tick, setTick] = useState(0)
  useEffect(() => {
    let live = true
    Promise.all([api.listRoles(), api.listPermissions()])
      .then(([r, p]) => {
        if (live) {
          setRoles(r)
          setCatalogue(p)
        }
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [tick])

  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      setTick((n) => n + 1)
    } catch (e) {
      setError(msg(e))
    }
  }

  const toggle = (list: string[], perm: string): string[] =>
    list.includes(perm) ? list.filter((p) => p !== perm) : [...list, perm]

  return (
    <div>
      <h1>Roles</h1>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <section>
        <h2>New role</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void act(async () => {
              await api.createRole({ name, description, permissions: picked })
              setName('')
              setDescription('')
              setPicked([])
            })
          }}
        >
          <label>
            Role name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            Description
            <input value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <fieldset>
            <legend>Permissions</legend>
            {catalogue.map((p) => (
              <label key={p.name} title={p.description}>
                <input
                  type="checkbox"
                  checked={picked.includes(p.name)}
                  onChange={() => setPicked((cur) => toggle(cur, p.name))}
                />{' '}
                {p.name}
              </label>
            ))}
          </fieldset>
          <button type="submit">Create role</button>
        </form>
      </section>

      <section>
        <h2>Existing roles</h2>
        {roles.map((r) => (
          <fieldset key={r.id} aria-label={`role ${r.name}`}>
            <legend>
              {r.name} {r.is_builtin && <em>(built-in)</em>}
            </legend>
            <p>{r.description}</p>
            {catalogue.map((p) => (
              <label key={p.name} title={p.description}>
                <input
                  type="checkbox"
                  checked={r.permissions.includes(p.name)}
                  disabled={r.name === 'admin'}
                  aria-label={`${r.name} ${p.name}`}
                  onChange={() =>
                    void act(() =>
                      api.updateRole(r.id, { permissions: toggle(r.permissions, p.name) }),
                    )
                  }
                />{' '}
                {p.name}
              </label>
            ))}
            {!r.is_builtin && (
              <button
                aria-label={`delete role ${r.name}`}
                onClick={() => {
                  if (window.confirm(`Delete role ${r.name}?`)) void act(() => api.deleteRole(r.id))
                }}
              >
                Delete
              </button>
            )}
          </fieldset>
        ))}
      </section>
    </div>
  )
}
