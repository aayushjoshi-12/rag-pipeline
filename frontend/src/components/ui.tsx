import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

/** Page title row: h1, one-line description, primary action(s) on the right. */
export function PageHeader({
  title,
  description,
  actions,
  back,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
  back?: ReactNode
}) {
  return (
    <header className="page-header">
      {back}
      <div className="page-header-row">
        <div className="page-header-text">
          <h1>{title}</h1>
          {description && <p className="page-desc">{description}</p>}
        </div>
        {actions && <div className="page-actions">{actions}</div>}
      </div>
    </header>
  )
}

/** A bordered surface with an optional header (title, caption, actions). */
export function Card({
  title,
  description,
  actions,
  children,
  flush = false,
  className = '',
  ...rest
}: {
  title?: string
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  /** No body padding (tables that run edge to edge). */
  flush?: boolean
  className?: string
  'aria-label'?: string
}) {
  return (
    <section className={`card ${className}`.trim()} {...rest}>
      {(title || actions) && (
        <div className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {description && <p className="card-desc">{description}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? 'card-body flush' : 'card-body'}>{children}</div>
    </section>
  )
}

export function Alert({
  children,
  tone = 'error',
}: {
  children: ReactNode
  tone?: 'error' | 'warning'
}) {
  return (
    <div role="alert" className={`alert alert-${tone}`}>
      <Icon name={tone === 'error' ? 'xCircle' : 'alert'} className="alert-icon" />
      <div className="alert-body">{children}</div>
    </div>
  )
}

export function EmptyState({
  icon = 'inbox',
  title,
  children,
  action,
}: {
  icon?: IconName
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name={icon} size={22} />
      </span>
      <p className="empty-title">{title}</p>
      {children && <p className="empty-text">{children}</p>}
      {action}
    </div>
  )
}

/** Placeholder rows while a list loads (decorative; the busy state is on the container). */
export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton-list" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton-row">
          <span className="skeleton" style={{ width: `${55 - (i % 3) * 12}%` }} />
          <span className="skeleton" style={{ width: '4rem' }} />
        </div>
      ))}
      <span className="sr-only">Loading…</span>
    </div>
  )
}

/** KPI tile. `children` is an optional footer (delta, budget meter...). */
export function Kpi({
  label,
  value,
  testId,
  hint,
  children,
}: {
  label: string
  value: ReactNode
  testId?: string
  hint?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" data-testid={testId}>
        {value}
      </div>
      {hint && <div className="kpi-hint">{hint}</div>}
      {children}
    </div>
  )
}

/** Card for one chart: title, small caption, legend slot, then the plot. Reusable by any chart. */
export function ChartCard({
  title,
  caption,
  legend,
  children,
}: {
  title: string
  caption?: ReactNode
  legend?: ReactNode
  children: ReactNode
}) {
  return (
    <figure className="chart-card">
      <figcaption className="chart-head">
        <span className="chart-title">{title}</span>
        {caption && <span className="chart-caption">{caption}</span>}
      </figcaption>
      {legend}
      {children}
    </figure>
  )
}
