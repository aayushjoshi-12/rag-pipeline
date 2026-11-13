import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import type { Chunk, DocumentDetail } from '../types'

export default function DocumentDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const [doc, setDoc] = useState<DocumentDetail | null>(null)
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [error, setError] = useState<string | null>(null)

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
  }, [id])

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
