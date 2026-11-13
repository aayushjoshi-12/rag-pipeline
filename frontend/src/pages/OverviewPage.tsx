import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import { useStore } from '../store'
import type { HealthResponse } from '../types'

export default function OverviewPage() {
  const { documents, refreshDocuments } = useStore()
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
  return (
    <div>
      <h1>Overview</h1>
      <section aria-label="health">
        <h2>System health</h2>
        {healthError && <p className="error">API unreachable: {healthError}</p>}
        {health && (
          <ul>
            {Object.entries(health.checks).map(([name, c]) => (
              <li key={name}>
                {name}: <StatusBadge status={c.ok ? 'ok' : 'failed'} /> <small>{c.detail}</small>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="counts">
        <h2>Documents</h2>
        <p>
          Total {documents.length} · ready {count('ready')} · ingesting {count('ingesting')} ·
          pending {count('pending')} · failed {count('failed')}
        </p>
      </section>
      <section aria-label="recent">
        <h2>Recent</h2>
        <ul>
          {documents.slice(0, 5).map((d) => (
            <li key={d.id}>
              {d.filename} <StatusBadge status={d.status} />
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
