import { useEffect, useRef } from 'react'
import { POLL_MS } from './config'
import { useStore } from './store'
import type { Job } from './types'

const TERMINAL = new Set(['done', 'failed'])

export const isTerminal = (status: string): boolean => TERMINAL.has(status)

/** Poll GET /jobs/{id} until it reaches done/failed, then call onSettled once. */
export function useJobPolling(jobId: string | null, onSettled?: (job: Job) => void): void {
  const pollJob = useStore((s) => s.pollJob)
  const settled = useRef(onSettled)
  useEffect(() => {
    settled.current = onSettled
  })
  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      try {
        const job = await pollJob(jobId)
        if (cancelled) return
        if (isTerminal(job.status)) {
          settled.current?.(job)
          return
        }
      } catch {
        if (cancelled) return
      }
      timer = setTimeout(() => void tick(), POLL_MS)
    }
    void tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId, pollJob])
}
