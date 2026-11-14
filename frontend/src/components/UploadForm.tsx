import { useRef, useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store'
import { Dropzone } from './Dropzone'
import { JobTracker } from './JobTracker'
import { Alert, Card } from './ui'

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
    <Card
      title="Upload files"
      description="Files are parsed, chunked and embedded in the background."
    >
      <form onSubmit={(e) => void onSubmit(e)} className="form" onChange={() => setError(null)}>
        <Dropzone
          inputRef={input}
          label="Document"
          multiple
          prompt="Drop files here or click to browse"
        />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={uploading}>
            {uploading ? 'Uploading…' : 'Upload'}
          </button>
          {uploading && <progress className="progress" aria-label="uploading" />}
        </div>
      </form>
      {error && <Alert>{error}</Alert>}
      {tracked.length > 0 && (
        <div className="jobs">
          {tracked.map((t) => (
            <JobTracker key={t.jobId} jobId={t.jobId} filename={t.filename} onSettled={onSettled} />
          ))}
        </div>
      )}
    </Card>
  )
}
