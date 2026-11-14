import { useEffect } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { RequirePermission } from './components/RequirePermission'
import ChatPage from './pages/ChatPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import DocumentsPage from './pages/DocumentsPage'
import LoginPage from './pages/LoginPage'
import OverviewPage from './pages/OverviewPage'
import RolesPage from './pages/RolesPage'
import TracesPage from './pages/TracesPage'
import UsagePage from './pages/UsagePage'
import UsersPage from './pages/UsersPage'
import { useAuth, useCan } from './store/auth'

function Shell() {
  const me = useAuth((s) => s.me)
  const logout = useAuth((s) => s.logout)
  const canDocs = useCan('documents:read')
  const canChat = useCan('chat:use')
  const canTraces = useCan('traces:read')
  const canUsers = useCan('users:manage')
  const canRoles = useCan('roles:manage')

  return (
    <div className="shell">
      <nav>
        <strong>rag-pipeline</strong>
        <NavLink to="/" end>
          Overview
        </NavLink>
        {canDocs && <NavLink to="/documents">Documents</NavLink>}
        {canChat && <NavLink to="/chat">Chat</NavLink>}
        {canTraces && <NavLink to="/traces">Traces</NavLink>}
        {canTraces && <NavLink to="/usage">Usage</NavLink>}
        {canUsers && <NavLink to="/users">Users</NavLink>}
        {canRoles && <NavLink to="/roles">Roles</NavLink>}
        <span className="spacer" />
        {me?.auth_enabled ? (
          <>
            <span data-testid="whoami">{me.email}</span>
            <button onClick={logout}>Sign out</button>
          </>
        ) : (
          <span title="The server runs with AUTH_ENABLED=false">auth disabled</span>
        )}
      </nav>
      <main>
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
            path="/roles"
            element={
              <RequirePermission permission="roles:manage">
                <RolesPage />
              </RequirePermission>
            }
          />
        </Routes>
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

  if (status === 'loading') return <p className="loading">Loading…</p>
  if (status === 'anonymous') return <LoginPage />
  return <Shell />
}
