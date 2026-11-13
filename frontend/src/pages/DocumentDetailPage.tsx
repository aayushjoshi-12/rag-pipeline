import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { JobTracker } from '../components/JobTracker'
import { StatusBadge } from '../components/StatusBadge'
import { useStore } from '../store'
import type { Chunk, DocumentDetail } from '../types'

export default function DocumentDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
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
      <p role="alert" className="error">
        {error}
      </p>
    )
  if (!doc) return <p>Loading…</p>

  return (
    <div>
      <p>
        <Link to="/documents">← Documents</Link>
      </p>
      <h1>{doc.filename}</h1>
      <StatusBadge status={doc.status} />
      <button
        onClick={() => {
          if (window.confirm(`Delete ${doc.filename}?`))
            void api.deleteDocument(doc.id).then(() => navigate('/documents'))
        }}
      >
        Delete
      </button>

      <section>
        <h2>Upload a new version</h2>
        <p>Only chunks whose content changed are re-embedded.</p>
        <form
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
          <label>
            New version
            <input ref={fileInput} type="file" accept=".pdf,.docx,.txt,.md" />
          </label>
          <button type="submit">Replace</button>
        </form>
        {replaceError && (
          <p role="alert" className="error">
            {replaceError}
          </p>
        )}
        {replacing && (
          <JobTracker
            key={replacing.jobId}
            jobId={replacing.jobId}
            filename={replacing.filename}
            onSettled={() => setReload((n) => n + 1)}
          />
        )}
      </section>

      <h2>Jobs</h2>
      <ul>
        {doc.jobs.map((j) => (
          <li key={j.id}>
            <StatusBadge status={j.status} /> {new Date(j.created_at).toLocaleString()}
            {j.error && <span className="error"> — {j.error}</span>}
          </li>
        ))}
      </ul>

      <h2>Chunks ({chunks.length})</h2>
      {chunks.length === 0 && <p>No chunks indexed.</p>}
      {chunks.map((c) => (
        <article key={c.id} className="chunk">
          <h3>Chunk {c.index}</h3>
          <p>{c.text}</p>
        </article>
      ))}
    </div>
  )
}
