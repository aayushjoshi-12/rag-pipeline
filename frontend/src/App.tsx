import { useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { Icon, type IconName } from './components/Icon'
import { RequirePermission } from './components/RequirePermission'
import ChatPage from './pages/ChatPage'
import ApiKeysPage from './pages/ApiKeysPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import DocumentsPage from './pages/DocumentsPage'
import LoginPage from './pages/LoginPage'
import OverviewPage from './pages/OverviewPage'
import RolesPage from './pages/RolesPage'
import SettingsPage from './pages/settings/SettingsPage'
import TracesPage from './pages/TracesPage'
import UsagePage from './pages/UsagePage'
import UsersPage from './pages/UsersPage'
import { useAuth, useCan } from './store/auth'

function NavItem({
  to,
  icon,
  label,
  end,
}: {
  to: string
  icon: IconName
  label: string
  end?: boolean
}) {
  return (
    <NavLink to={to} end={end} className="nav-link">
      <Icon name={icon} />
      <span>{label}</span>
    </NavLink>
  )
}

function Shell() {
  const me = useAuth((s) => s.me)
  const logout = useAuth((s) => s.logout)
  const canDocs = useCan('documents:read')
  const canChat = useCan('chat:use')
  const canTraces = useCan('traces:read')
  const canUsers = useCan('users:manage')
  const canRoles = useCan('roles:manage')
  const canKeys = useCan('apikeys:manage')
  const canSettingsRead = useCan('settings:read')
  const canSettingsManage = useCan('settings:manage')
  const canSettings = canSettingsRead || canSettingsManage
  const [open, setOpen] = useState(false)

  return (
    <div className={open ? 'shell nav-open' : 'shell'}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="topbar">
        <span className="brand">
          <span className="brand-mark">
            <Icon name="logo" size={16} />
          </span>
          rag-pipeline
        </span>
        <button
          className="icon-btn"
          aria-label={open ? 'Close menu' : 'Open menu'}
          aria-expanded={open}
          aria-controls="sidebar"
          onClick={() => setOpen((v) => !v)}
        >
          <Icon name={open ? 'close' : 'menu'} size={20} />
        </button>
      </div>
      <button className="scrim" tabIndex={-1} aria-hidden="true" onClick={() => setOpen(false)} />
      <aside id="sidebar" className="sidebar">
        <div className="brand sidebar-brand">
          <span className="brand-mark">
            <Icon name="logo" size={16} />
          </span>
          rag-pipeline
        </div>
        <nav aria-label="Main" className="nav" onClick={() => setOpen(false)}>
          <div className="nav-group">Workspace</div>
          <NavItem to="/" end icon="overview" label="Overview" />
          {canDocs && <NavItem to="/documents" icon="documents" label="Documents" />}
          {canChat && <NavItem to="/chat" icon="chat" label="Chat" />}
          {canTraces && (
            <>
              <div className="nav-group">Observability</div>
              <NavItem to="/traces" icon="traces" label="Traces" />
              <NavItem to="/usage" icon="usage" label="Usage" />
            </>
          )}
          {(canUsers || canRoles || canKeys || canSettings) && (
            <div className="nav-group">Admin</div>
          )}
          {canUsers && <NavItem to="/users" icon="users" label="Users" />}
          {canRoles && <NavItem to="/roles" icon="roles" label="Roles" />}
          {canKeys && <NavItem to="/api-keys" icon="key" label="API keys" />}
          {canSettings && <NavItem to="/settings" icon="settings" label="Settings" />}
        </nav>
        <div className="sidebar-foot">
          {me?.auth_enabled ? (
            <>
              <div className="whoami">
                <span className="avatar" aria-hidden="true">
                  {me.email.slice(0, 1).toUpperCase()}
                </span>
                <span className="whoami-email" data-testid="whoami" title={me.email}>
                  {me.email}
                </span>
              </div>
              <button className="btn btn-secondary btn-block" onClick={logout}>
                Sign out
              </button>
            </>
          ) : (
            <span className="muted small" title="The server runs with AUTH_ENABLED=false">
              auth disabled
            </span>
          )}
        </div>
      </aside>
      <main id="main" className="main" tabIndex={-1}>
        <div className="page">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route
              path="/documents"
              element={
                <RequirePermission permission="documents:read">
                  <DocumentsPage />
                </RequirePermission>
              }
            />
            <Route
              path="/documents/:id"
              element={
                <RequirePermission permission="documents:read">
                  <DocumentDetailPage />
                </RequirePermission>
              }
            />
            <Route
              path="/chat"
              element={
                <RequirePermission permission="chat:use">
                  <ChatPage />
                </RequirePermission>
              }
            />
            <Route
              path="/traces"
              element={
                <RequirePermission permission="traces:read">
                  <TracesPage />
                </RequirePermission>
              }
            />
            <Route
              path="/usage"
              element={
                <RequirePermission permission="traces:read">
                  <UsagePage />
                </RequirePermission>
              }
            />
            <Route
              path="/users"
              element={
                <RequirePermission permission="users:manage">
                  <UsersPage />
                </RequirePermission>
              }
            />
            <Route
              path="/api-keys"
              element={
                <RequirePermission permission="apikeys:manage">
                  <ApiKeysPage />
                </RequirePermission>
              }
            />
            <Route
              path="/settings"
              element={
                <RequirePermission permission={['settings:read', 'settings:manage']}>
                  <SettingsPage />
                </RequirePermission>
              }
            />
            <Route
              path="/roles"
              element={
                <RequirePermission permission="roles:manage">
                  <RolesPage />
                </RequirePermission>
              }
            />
          </Routes>
        </div>
      </main>
    </div>
  )
}

export default function App() {
  const status = useAuth((s) => s.status)
  const bootstrap = useAuth((s) => s.bootstrap)
  useEffect(() => {
    if (status === 'loading') void bootstrap()
  }, [status, bootstrap])

  if (status === 'loading')
    return (
      <div className="boot">
        <span className="spinner" aria-hidden="true" />
        <span>Loading…</span>
      </div>
    )
  if (status === 'anonymous') return <LoginPage />
  return <Shell />
}
