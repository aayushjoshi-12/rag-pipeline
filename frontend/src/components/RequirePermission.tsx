import type { ReactNode } from 'react'
import { useAuth } from '../store/auth'
import { Alert } from './ui'

/** Renders children only when the user holds the permission; otherwise an explicit notice
 * (so a deep link to a forbidden page explains itself instead of showing a blank page). */
export function RequirePermission({
  permission,
  children,
}: {
  /** One permission, or several of which any one is enough. */
  permission: string | string[]
  children: ReactNode
}) {
  const needed = Array.isArray(permission) ? permission : [permission]
  const allowed = useAuth((s) => needed.some((p) => s.me?.permissions.includes(p) ?? false))
  if (!allowed)
    return <Alert>You do not have permission to view this page ({needed.join(' or ')}).</Alert>
  return <>{children}</>
}
