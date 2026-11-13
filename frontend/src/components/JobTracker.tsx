import { useJobPolling } from '../hooks'
import { useStore } from '../store'
import type { Job } from '../types'
import { StatusBadge } from './StatusBadge'

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
        <span>{filename}</span>
        <StatusBadge status={status} />
      </div>
      <progress value={pct} max={100} aria-label={`progress ${filename}`} />
      {job?.stats && status === 'done' && (
        <p className="stats" data-testid="job-stats">
          {job.stats.mode === 'incremental'
            ? `Updated: re-embedded ${job.stats.embedded} of ${job.stats.chunks_total} chunks (${job.stats.kept} unchanged, ${job.stats.removed} removed)`
            : `Indexed ${job.stats.chunks_total} chunks`}
        </p>
      )}
      {job?.error && (
        <p role="alert" className="error">
          {job.error}
        </p>
      )}
    </div>
  )
}
