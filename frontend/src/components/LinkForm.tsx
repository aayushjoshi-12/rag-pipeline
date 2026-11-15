import { useState } from 'react'
import { api } from '../api/client'
import { useStore } from '../store'
import { TrackedJobs, type Tracked } from './JobTracker'
import { Alert, Card } from './ui'

/** Ingest a document from an http(s) link or s3:// object. The server downloads it in the worker. */
export function LinkForm() {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tracked, setTracked] = useState<Tracked[]>([])
  const trackJob = useStore((s) => s.trackJob)
  const refresh = useStore((s) => s.refreshDocuments)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const value = url.trim()
    if (!value) {
      setError('Enter a link first.')
      return
    }
    setError(null)
    setBusy(true)
    try {
      const res = await api.ingestUrl(value)
      trackJob(res.job)
      setTracked((t) => [{ jobId: res.job.id, filename: res.document.filename }, ...t])
      setUrl('')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      title="Add from link"
      description="Paste a web link (https://…) or an s3:// object. The server downloads it in the background, then indexes it."
      aria-label="Add from link"
    >
      <form onSubmit={(e) => void onSubmit(e)} className="form" noValidate>
        <div className="field">
          <label className="field-label" htmlFor="document-url">
            Document URL
          </label>
          <input
            id="document-url"
            className="input"
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder="https://example.com/report.pdf"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value)
              setError(null)
            }}
            aria-describedby="document-url-help"
          />
          <p id="document-url-help" className="field-help">
            PDF, DOCX, TXT or Markdown. Links to private or internal addresses are refused.
          </p>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Adding…' : 'Add link'}
          </button>
        </div>
      </form>
      {error && <Alert>{error}</Alert>}
      <TrackedJobs tracked={tracked} onSettled={() => void refresh()} />
    </Card>
  )
}
