import type { ReactNode } from 'react'

/** A plain accessible table (the "table view" of a chart): numbers right-aligned. */
export function DataTable({
  label,
  head,
  rows,
}: {
  label: string
  head: { text: string; num?: boolean }[]
  rows: { key: string; cells: ReactNode[] }[]
}) {
  return (
    <div className="table-wrap">
      <table className="table" aria-label={label}>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h.text} className={h.num ? 'num-col' : undefined}>
                {h.text}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={head.length} className="muted">
                No data in this period.
              </td>
            </tr>
          ) : (
            rows.map((r) => (
              <tr key={r.key}>
                {r.cells.map((c, i) => (
                  <td key={i} className={head[i]?.num ? 'num-col nowrap' : undefined}>
                    {c}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}

/** The "Table view" disclosure every tab ends with: the numbers behind its charts. */
export function TableView({ children }: { children: ReactNode }) {
  return (
    <details className="card disclosure">
      <summary>Table view</summary>
      <div className="table-stack">{children}</div>
    </details>
  )
}
