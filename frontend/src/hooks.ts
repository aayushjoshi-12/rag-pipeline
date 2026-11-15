import { useEffect, useRef, useState } from 'react'
import { POLL_MS } from './config'
import { msg } from './lib'
import { useStore } from './store'
import type { Job } from './types'

const TERMINAL = new Set(['done', 'failed'])

const isTerminal = (status: string): boolean => TERMINAL.has(status)

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

/**
 * Admin-page data loading: run ``load`` on mount and whenever ``key`` changes or ``reload`` is
 * called, hand the result to ``apply`` (skipped once unmounted), and expose ``act`` to run a
 * mutation that clears the error, then reloads on success or shows the message on failure.
 */
export function useLoadAct<T>(
  load: () => Promise<T>,
  apply: (data: T) => void,
  key: unknown = null,
) {
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const latest = useRef({ load, apply })
  useEffect(() => {
    latest.current = { load, apply }
  })
  useEffect(() => {
    let live = true
    latest.current
      .load()
      .then((data) => {
        if (live) latest.current.apply(data)
      })
      .catch((e: unknown) => {
        if (live) setError(msg(e))
      })
    return () => {
      live = false
    }
  }, [key, tick])
  const reload = () => setTick((n) => n + 1)
  const act = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      reload()
    } catch (e) {
      setError(msg(e))
    }
  }
  return { error, setError, act, reload }
}
