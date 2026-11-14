import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { StatusBadge } from '../components/StatusBadge'
import { UploadForm } from '../components/UploadForm'
import { useStore } from '../store'
import { useCan } from '../store/auth'

export default function DocumentsPage() {
  const { documents, loading, error, refreshDocuments, removeDocument } = useStore()
  const canWrite = useCan('documents:write')
  const canDelete = useCan('documents:delete')
  useEffect(() => {
    void refreshDocuments()
  }, [refreshDocuments])

  return (
    <div>
      <h1>Documents</h1>
      {canWrite && <UploadForm />}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {loading && documents.length === 0 ? (
        <p>Loading…</p>
      ) : documents.length === 0 ? (
        <p>No documents yet{canWrite ? ' — upload one above.' : '.'}</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Created</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {documents.map((d) => (
              <tr key={d.id}>
                <td>
                  <Link to={`/documents/${d.id}`}>{d.filename}</Link>
                </td>
                <td>
                  <StatusBadge status={d.status} />
                </td>
                <td>{new Date(d.created_at).toLocaleString()}</td>
                <td>
                  {canDelete && (
                    <button
                      aria-label={`delete ${d.filename}`}
                      onClick={() => {
                        if (window.confirm(`Delete ${d.filename}?`)) void removeDocument(d.id)
                      }}
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
