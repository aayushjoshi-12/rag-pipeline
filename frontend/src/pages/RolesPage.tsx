import { useState } from 'react'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { Alert, Card, PageHeader } from '../components/ui'
import { useLoadAct } from '../hooks'
import { toggle } from '../lib'
import type { PermissionInfo, Role } from '../types'

export default function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([])
  const [catalogue, setCatalogue] = useState<PermissionInfo[]>([])
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [picked, setPicked] = useState<string[]>([])

  const { error, act } = useLoadAct(
    () => Promise.all([api.listRoles(), api.listPermissions()]),
    ([r, p]) => {
      setRoles(r)
      setCatalogue(p)
    },
  )

  return (
    <div className="stack">
      <PageHeader
        title="Roles"
        description="Bundle permissions into roles and assign them to users."
      />
      {error && <Alert>{error}</Alert>}

      <Card title="New role" description="Choose exactly the permissions this role should grant.">
        <form
          className="form form-grid"
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
          <label className="field">
            <span className="field-label">Role name</span>
            <input
              className="input"
              autoComplete="off"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </label>
          <label className="field">
            <span className="field-label">Description</span>
            <input
              className="input"
              autoComplete="off"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <fieldset className="fieldset span-all">
            <legend>Permissions</legend>
            <div className="checks checks-grid">
              {catalogue.map((p) => (
                <label key={p.name} className="check" title={p.description}>
                  <input
                    type="checkbox"
                    checked={picked.includes(p.name)}
                    onChange={() => setPicked((cur) => toggle(cur, p.name))}
                  />
                  <span>{p.name}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="form-actions span-all">
            <button type="submit" className="btn btn-primary">
              Create role
            </button>
          </div>
        </form>
      </Card>

      <section className="stack" aria-labelledby="existing-roles">
        <h2 id="existing-roles" className="section-title">
          Role catalogue
        </h2>
        <div className="role-grid">
          {roles.map((r) => (
            <fieldset key={r.id} className="card role-card" aria-label={`role ${r.name}`}>
              <legend className="sr-only">
                {r.name} {r.is_builtin && <em>(built-in)</em>}
              </legend>
              <div className="card-head">
                <div>
                  <h3 className="card-title">{r.name}</h3>
                  <p className="card-desc">{r.description || 'No description'}</p>
                </div>
                <div className="card-actions">
                  {r.is_builtin ? (
                    <span className="tag">
                      <Icon name="lock" size={12} /> built-in
                    </span>
                  ) : (
                    <button
                      className="btn btn-ghost btn-danger-text btn-sm"
                      aria-label={`delete role ${r.name}`}
                      onClick={() => {
                        if (window.confirm(`Delete role ${r.name}?`))
                          void act(() => api.deleteRole(r.id))
                      }}
                    >
                      <Icon name="trash" size={15} />
                      Delete
                    </button>
                  )}
                </div>
              </div>
              <div className="card-body checks checks-grid">
                {catalogue.map((p) => (
                  <label key={p.name} className="check" title={p.description}>
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
                    />
                    <span>{p.name}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
        </div>
      </section>
    </div>
  )
}
