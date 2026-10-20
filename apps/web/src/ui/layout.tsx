import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { cn } from './cn';

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {description !== undefined && <p className="mt-1 max-w-3xl text-muted">{description}</p>}
      </div>
      {actions}
    </header>
  );
}

export function Section({
  title,
  children,
  className,
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn('mb-6', className)}>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export type BadgeTone = 'neutral' | 'success' | 'danger' | 'warning' | 'accent';

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: 'border-line text-muted',
  success: 'border-success text-success',
  danger: 'border-danger text-danger',
  warning: 'border-warning text-warning',
  accent: 'border-accent text-accent',
};

export function Badge({ tone = 'neutral', children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        BADGE_TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

/** A planned screen from the design inventory, shown until it is built. */
export function PlaceholderSection({
  area,
  screen,
  content,
}: {
  area: string;
  screen: string;
  content: string;
}) {
  return (
    <section
      aria-label={`${area}: ${screen}`}
      className="rounded-md border border-dashed border-line bg-surface p-3"
    >
      <h3 className="text-sm font-medium">{screen}</h3>
      <p className="mt-1 text-xs text-muted">{content}</p>
      <p className="mt-2 text-xs text-muted">Planned</p>
    </section>
  );
}

/** Renders loading, error and ready states of a query. */
export function QueryBoundary<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) {
    return (
      <p role="status" className="text-muted">
        Loading...
      </p>
    );
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-danger">
        {query.error.message}
      </p>
    );
  }
  return <>{children(query.data)}</>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="rounded-md border border-line bg-surface p-4 text-muted">{children}</p>;
}
