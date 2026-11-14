import { useJobPolling } from '../hooks'
import { useStore } from '../store'
import type { Job } from '../types'
import { StatusBadge } from './StatusBadge'
import { Alert } from './ui'

const formatBytes = (n: number): string =>
  n >= 1_048_576 ? `${(n / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`

const STEPS = ['pending', 'running', 'done'] as const

/** Live status of one ingest job, polled until it settles. */
export function JobTracker({
  jobId,
  filename,
  onSettled,
}: {
  jobId: string
  filename: string
  onSettled?: (job: Job) => void
}) {
  useJobPolling(jobId, onSettled)
  const job = useStore((s) => s.jobs[jobId])
  const status = job?.status ?? 'pending'
  const stepIndex =
    status === 'failed' ? STEPS.length : STEPS.indexOf(status as (typeof STEPS)[number])
  const pct = status === 'failed' ? 100 : Math.round(((stepIndex + 1) / STEPS.length) * 100)
  return (
    <div className="job" aria-label={`job ${filename}`}>
      <div className="job-head">
        <span className="truncate job-name" title={filename}>
          {filename}
        </span>
        <StatusBadge status={status} />
      </div>
      <progress
        className={`progress progress-${status}`}
        value={pct}
        max={100}
        aria-label={`progress ${filename}`}
      />
      {job?.stats?.source_url && status !== 'pending' && (
        <p className="stats muted small" data-testid="job-source">
          From <span className="truncate">{job.stats.source_url}</span>
          {job.stats.bytes_downloaded !== undefined &&
            ` · ${formatBytes(job.stats.bytes_downloaded)} downloaded`}
        </p>
      )}
      {job?.stats && status === 'done' && (
        <p className="stats muted small" data-testid="job-stats">
          {job.stats.mode === 'incremental'
            ? `Updated: re-embedded ${job.stats.embedded} of ${job.stats.chunks_total} chunks (${job.stats.kept} unchanged, ${job.stats.removed} removed)`
            : `Indexed ${job.stats.chunks_total} chunks`}
        </p>
      )}
      {job?.error && <Alert>{job.error}</Alert>}
    </div>
  )
}
