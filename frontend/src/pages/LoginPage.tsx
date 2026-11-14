import { useState } from 'react'
import { Icon } from '../components/Icon'
import { Alert } from '../components/ui'
import { useAuth } from '../store/auth'

export default function LoginPage() {
  const { login, error } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)

  return (
    <div className="login-screen">
      <div className="login">
        <div className="login-brand">
          <span className="brand-mark brand-mark-lg">
            <Icon name="logo" size={24} />
          </span>
          <span className="login-name">rag-pipeline</span>
        </div>
        <h1>Sign in</h1>
        <p className="page-desc">Use your workspace account to continue.</p>
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault()
            setBusy(true)
            void login(email, password).finally(() => setBusy(false))
          }}
        >
          <label className="field">
            <span className="field-label">Email</span>
            <input
              className="input"
              type="email"
              autoComplete="username"
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
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <Alert>{error}</Alert>}
          <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
