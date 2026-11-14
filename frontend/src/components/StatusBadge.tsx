import { Icon, type IconName } from './Icon'

const ICONS: Record<string, IconName> = {
  ready: 'checkCircle',
  done: 'checkCircle',
  ok: 'checkCircle',
  failed: 'xCircle',
  error: 'xCircle',
  running: 'spinner',
  ingesting: 'spinner',
  pending: 'clock',
}

/** Status pill: icon + word (never colour alone). Text content is exactly the status. */
export function StatusBadge({ status }: { status: string }) {
  const spin = status === 'running' || status === 'ingesting'
  return (
    <span className={`badge badge-${status}`} data-testid="status-badge">
      <Icon name={ICONS[status] ?? 'clock'} size={13} className={spin ? 'spin' : undefined} />
      {status}
    </span>
  )
}
