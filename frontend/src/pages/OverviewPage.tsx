import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Link } from 'react-router-dom'
import { Icon } from '../components/Icon'
import { StatusBadge } from '../components/StatusBadge'
import { Alert, Card, EmptyState, Kpi, PageHeader, SkeletonRows } from '../components/ui'
import { useStore } from '../store'
import { useCan } from '../store/auth'
import type { HealthResponse } from '../types'

export default function OverviewPage() {
  const { documents, refreshDocuments } = useStore()
  const canChat = useCan('chat:use')
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)

  useEffect(() => {
    void refreshDocuments()
    api
      .health()
      .then(setHealth)
      .catch((e: unknown) => setHealthError(e instanceof Error ? e.message : String(e)))
  }, [refreshDocuments])

  const count = (s: string) => documents.filter((d) => d.status === s).length
  const inFlight = count('ingesting') + count('pending')
  const total = documents.length
  const seg = (n: number) => (total ? `${(n / total) * 100}%` : '0%')
  return (
    <div className="stack">
      <PageHeader
        title="Overview"
        description="System health and the state of your document library at a glance."
        actions={
          canChat && (
            <Link to="/chat" className="btn btn-primary">
              <Icon name="chat" size={15} />
              Ask a question
            </Link>
          )
        }
      />

      <div className="kpi-grid">
        <Kpi label="Documents" value={total.toLocaleString()} />
        <Kpi label="Ready" value={count('ready').toLocaleString()} />
        <Kpi label="In progress" value={inFlight.toLocaleString()} />
        <Kpi label="Failed" value={count('failed').toLocaleString()} />
      </div>

      <div className="grid-2">
        <Card title="System health" aria-label="health">
          {healthError && <Alert>API unreachable: {healthError}</Alert>}
          {!health && !healthError && <SkeletonRows rows={4} />}
          {health && (
            <ul className="health-list">
              {Object.entries(health.checks).map(([name, c]) => (
                <li key={name}>
                  <span className={c.ok ? 'dot dot-ok' : 'dot dot-failed'} aria-hidden="true" />
                  <span className="health-name">{name}</span>
                  <small className="muted truncate" title={c.detail}>
                    {c.detail}
                  </small>
                  <StatusBadge status={c.ok ? 'ok' : 'failed'} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Library" description="Index state across all documents." aria-label="counts">
          <div
            className="stackbar"
            role="img"
            aria-label={`${count('ready')} ready, ${inFlight} in progress, ${count('failed')} failed`}
          >
            <span className="stackbar-ready" style={{ width: seg(count('ready')) }} />
            <span className="stackbar-flight" style={{ width: seg(inFlight) }} />
            <span className="stackbar-failed" style={{ width: seg(count('failed')) }} />
          </div>
          <p className="muted small summary-line">
            Total {total} · ready {count('ready')} · ingesting {count('ingesting')} · pending{' '}
            {count('pending')} · failed {count('failed')}
          </p>
        </Card>
      </div>

      <Card
        title="Recently added"
        aria-label="recent"
        flush
        actions={
          <Link to="/documents" className="link-sm">
            View all
          </Link>
        }
      >
        {documents.length === 0 ? (
          <EmptyState icon="documents" title="Nothing indexed yet">
            Uploaded documents show up here with their ingestion status.
          </EmptyState>
        ) : (
          <ul className="list">
            {documents.slice(0, 5).map((d) => (
              <li key={d.id}>
                <Icon name="file" size={16} />
                <span className="truncate" title={d.filename}>
                  {d.filename}
                </span>
                <StatusBadge status={d.status} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
