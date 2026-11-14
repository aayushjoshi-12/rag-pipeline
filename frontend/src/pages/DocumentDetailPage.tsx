import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { Dropzone } from '../components/Dropzone'
import { Icon } from '../components/Icon'
import { JobTracker } from '../components/JobTracker'
import { StatusBadge } from '../components/StatusBadge'
import { Alert, Card, PageHeader, SkeletonRows } from '../components/ui'
import { useStore } from '../store'
import { useCan } from '../store/auth'
import type { Chunk, DocumentDetail } from '../types'

export default function DocumentDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const canWrite = useCan('documents:write')
  const canDelete = useCan('documents:delete')
  const [doc, setDoc] = useState<DocumentDetail | null>(null)
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const [replacing, setReplacing] = useState<{ jobId: string; filename: string } | null>(null)
  const [replaceError, setReplaceError] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const trackJob = useStore((st) => st.trackJob)

  useEffect(() => {
    let live = true
    Promise.all([api.getDocument(id), api.listChunks(id)])
      .then(([d, c]) => {
        if (!live) return
        setDoc(d)
        setChunks(c)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      live = false
    }
  }, [id, reload])

  if (error)
    return (
      <div className="stack">
        <Alert>{error}</Alert>
      </div>
    )
  if (!doc)
    return (
      <div className="stack">
        <Card>
          <SkeletonRows rows={5} />
        </Card>
      </div>
    )

  return (
    <div className="stack">
      <PageHeader
        title={doc.filename}
        description={
          <span className="inline-meta">
            <StatusBadge status={doc.status} />
            <span>{doc.content_type}</span>
            <span>Added {new Date(doc.created_at).toLocaleString()}</span>
          </span>
        }
        back={
          <Link to="/documents" className="back-link">
            <Icon name="arrowLeft" size={15} />
            Back to documents
          </Link>
        }
        actions={
          canDelete && (
            <button
              className="btn btn-danger"
              onClick={() => {
                if (window.confirm(`Delete ${doc.filename}?`))
                  void api.deleteDocument(doc.id).then(() => navigate('/documents'))
              }}
            >
              <Icon name="trash" size={15} />
              Delete
            </button>
          )
        }
      />

      {canWrite && (
        <Card
          title="Upload a new version"
          description="Only chunks whose content changed are re-embedded."
        >
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault()
              const file = fileInput.current?.files?.[0]
              if (!file) {
                setReplaceError('Choose a file first.')
                return
              }
              setReplaceError(null)
              api
                .replace(doc.id, file)
                .then((res) => {
                  trackJob(res.job)
                  setReplacing({ jobId: res.job.id, filename: res.document.filename })
                })
                .catch((err: unknown) =>
                  setReplaceError(err instanceof Error ? err.message : String(err)),
                )
            }}
          >
            <Dropzone
              inputRef={fileInput}
              label="New version"
              prompt="Drop the new version here or click to browse"
            />
            <div className="form-actions">
              <button type="submit" className="btn btn-primary">
                Replace
              </button>
            </div>
          </form>
          {replaceError && <Alert>{replaceError}</Alert>}
          {replacing && (
            <div className="jobs">
              <JobTracker
                key={replacing.jobId}
                jobId={replacing.jobId}
                filename={replacing.filename}
                onSettled={() => setReload((n) => n + 1)}
              />
            </div>
          )}
        </Card>
      )}

      <Card title="Ingestion history" description="Every parse and embed run for this document.">
        {doc.jobs.length === 0 ? (
          <p className="muted">No ingestion jobs recorded.</p>
        ) : (
          <ul className="timeline">
            {doc.jobs.map((j) => (
              <li key={j.id}>
                <StatusBadge status={j.status} />
                <span className="num muted">{new Date(j.created_at).toLocaleString()}</span>
                {j.stats && (
                  <span className="muted small">
                    {j.stats.mode}, {j.stats.chunks_total} chunks
                  </span>
                )}
                {j.error && <span className="timeline-error"> — {j.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title={`Chunks (${chunks.length})`} description="The passages the retriever searches.">
        {chunks.length === 0 && <p className="muted">No chunks indexed.</p>}
        <div className="chunks">
          {chunks.map((c) => (
            <article key={c.id} className="chunk">
              <h3>Chunk {c.index}</h3>
              <p>{c.text}</p>
            </article>
          ))}
        </div>
      </Card>
    </div>
  )
}
