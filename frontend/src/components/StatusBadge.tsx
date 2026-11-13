export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge badge-${status}`} data-testid="status-badge">
      {status}
    </span>
  )
}
