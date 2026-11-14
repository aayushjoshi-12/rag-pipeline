import type { ReactNode } from 'react'
import { useCan } from '../store/auth'

/** Renders children only when the user holds the permission; otherwise an explicit notice
 * (so a deep link to a forbidden page explains itself instead of showing a blank page). */
export function RequirePermission({
  permission,
  children,
}: {
  permission: string
  children: ReactNode
}) {
  const allowed = useCan(permission)
  if (!allowed)
    return (
      <p role="alert" className="error">
        You do not have permission to view this page ({permission}).
      </p>
    )
  return <>{children}</>
}
