import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from '../components/Icon'
import { StatusBadge } from '../components/StatusBadge'
import { UploadForm } from '../components/UploadForm'
import { Alert, Card, EmptyState, PageHeader, SkeletonRows } from '../components/ui'
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
    <div className="stack">
      <PageHeader
        title="Documents"
        description="Everything that has been uploaded, parsed and indexed for search."
        actions={
          <button
            className="btn btn-secondary"
            onClick={() => void refreshDocuments()}
            disabled={loading}
          >
            <Icon name="refresh" size={15} />
            Refresh
          </button>
        }
      />
      {canWrite && <UploadForm />}
      {error && <Alert>{error}</Alert>}
      <Card title="Library" description={`${documents.length} in total`} flush>
        {loading && documents.length === 0 ? (
          <SkeletonRows />
        ) : documents.length === 0 ? (
          <EmptyState icon="documents" title="No documents yet">
            {canWrite
              ? 'Drop a file in the upload area above to index your first document.'
              : 'Documents will appear here once someone uploads them.'}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Created</th>
                  <th className="col-actions">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {documents.map((d) => (
                  <tr key={d.id}>
                    <td className="cell-main">
                      <Link to={`/documents/${d.id}`} className="file-link" title={d.filename}>
                        <Icon name="file" size={16} />
                        <span className="truncate">{d.filename}</span>
                      </Link>
                    </td>
                    <td>
                      <StatusBadge status={d.status} />
                    </td>
                    <td className="muted nowrap num">{new Date(d.created_at).toLocaleString()}</td>
                    <td className="col-actions">
                      {canDelete && (
                        <button
                          className="btn btn-ghost btn-danger-text btn-sm"
                          aria-label={`delete ${d.filename}`}
                          onClick={() => {
                            if (window.confirm(`Delete ${d.filename}?`)) void removeDocument(d.id)
                          }}
                        >
                          <Icon name="trash" size={15} />
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
