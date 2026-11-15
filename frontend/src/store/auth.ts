import { create } from 'zustand'
import { api } from '../api/client'
import { clearTokens, getTokens, onSessionLost, setTokens } from '../auth/session'
import type { Me } from '../types'

type Status = 'loading' | 'anonymous' | 'authenticated'

interface AuthState {
  status: Status
  me: Me | null
  error: string | null
  /** Resolve the current session from stored tokens (or an auth-disabled server). */
  bootstrap: () => Promise<void>
  login: (email: string, password: string) => Promise<void>
  logout: () => void
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export const useAuth = create<AuthState>((set) => ({
  status: 'loading',
  me: null,
  error: null,

  bootstrap: async () => {
    // No stored session: only probe /auth/me when the server says auth is off; on an auth-enabled server the
    // probe would just 401 (and log a console error). /health is public. If it cannot tell, fall back to the probe.
    if (!hasStoredSession()) {
      try {
        if ((await api.health()).auth_enabled !== false) {
          set({ status: 'anonymous', me: null })
          return
        }
      } catch {
        /* unknown: fall through to the /auth/me probe */
      }
    }
    try {
      // Works with no token when the server runs with AUTH_ENABLED=false.
      set({ status: 'authenticated', me: await api.me(), error: null })
    } catch {
      clearTokens()
      set({ status: 'anonymous', me: null })
    }
  },

  login: async (email, password) => {
    set({ error: null })
    try {
      const pair = await api.login(email, password)
      setTokens({ access: pair.access_token, refresh: pair.refresh_token })
      set({ status: 'authenticated', me: await api.me() })
    } catch (e) {
      clearTokens()
      set({ status: 'anonymous', me: null, error: message(e) })
    }
  },

  logout: () => {
    clearTokens()
    set({ status: 'anonymous', me: null, error: null })
  },
}))

// A request that could not be rescued by a token refresh ends the session.
onSessionLost(() => useAuth.setState({ status: 'anonymous', me: null }))

/** True when the signed-in user holds ``permission`` (always true with auth disabled). */
export function useCan(permission: string): boolean {
  return useAuth((s) => s.me?.permissions.includes(permission) ?? false)
}

export const hasStoredSession = (): boolean => getTokens() !== null
