import { useRef, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store'
import { JobTracker } from './JobTracker'

interface Tracked {
  jobId: string
  filename: string
}

export function UploadForm() {
  const input = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tracked, setTracked] = useState<Tracked[]>([])
  const trackJob = useStore((s) => s.trackJob)
  const refresh = useStore((s) => s.refreshDocuments)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const files = Array.from(input.current?.files ?? [])
    if (files.length === 0) {
      setError('Choose a file first.')
      return
    }
    setError(null)
    setUploading(true)
    for (const file of files) {
      try {
        const res = await api.upload(file)
        trackJob(res.job)
        setTracked((t) => [{ jobId: res.job.id, filename: res.document.filename }, ...t])
      } catch (err) {
        setError(`${file.name}: ${err instanceof Error ? err.message : String(err)}`)
      }
    }
    if (input.current) input.current.value = ''
    setUploading(false)
    await refresh()
  }

  const onSettled = () => void refresh()

  return (
    <section>
      <form onSubmit={(e) => void onSubmit(e)} className="upload">
        <label>
          Document
          <input ref={input} type="file" multiple accept=".pdf,.docx,.txt,.md" />
        </label>
        <button type="submit" disabled={uploading}>
          {uploading ? 'Uploading…' : 'Upload'}
        </button>
        {uploading && <progress aria-label="uploading" />}
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {tracked.map((t) => (
        <JobTracker key={t.jobId} jobId={t.jobId} filename={t.filename} onSettled={onSettled} />
      ))}
    </section>
  )
}
