/** Token storage + the "session lost" signal. Kept out of the API client so tests can mock it
 * independently. Tokens live in localStorage (survives reloads); a failure to read or write
 * storage (private mode, blocked site data) degrades to an in-memory session. */

const KEY = 'rag.tokens'

export interface Tokens {
  access: string
  refresh: string
}

let current: Tokens | null = null
let loaded = false
const lostListeners = new Set<() => void>()

function read(): Tokens | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (
      parsed &&
      typeof parsed === 'object' &&
      typeof (parsed as Tokens).access === 'string' &&
      typeof (parsed as Tokens).refresh === 'string'
    )
      return parsed as Tokens
  } catch {
    /* unavailable or corrupt: treat as signed out */
  }
  return null
}

export function getTokens(): Tokens | null {
  if (!loaded) {
    current = read()
    loaded = true
  }
  return current
}

export function setTokens(tokens: Tokens): void {
  current = tokens
  loaded = true
  try {
    localStorage.setItem(KEY, JSON.stringify(tokens))
  } catch {
    /* in-memory only */
  }
}

export function clearTokens(): void {
  current = null
  loaded = true
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* nothing to clear */
  }
}

/** Fired when a request proves the session is dead (401 and the refresh failed too). */
export function onSessionLost(listener: () => void): () => void {
  lostListeners.add(listener)
  return () => lostListeners.delete(listener)
}

export function notifySessionLost(): void {
  lostListeners.forEach((l) => l())
}
