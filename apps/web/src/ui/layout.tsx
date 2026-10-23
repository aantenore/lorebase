import type { UseQueryResult } from '@tanstack/react-query';
import type { ElementType, ReactNode } from 'react';
import { common as commonMessages } from '@/messages/common';
import { cn } from './cn';
import { LearnMore } from './learn-more';

/*
 * Layout primitives. Screens compose these instead of spacing and framing boxes themselves, so
 * every screen shares the same rhythm: 24px between page sections (`mb-6`), 16px inside cards
 * and between cards (`p-4`, `gap-4`), 12px between controls of a form row (`gap-3`).
 */

/**
 * Title of a screen, an optional one-line description with a "Learn more" link into the docs,
 * and actions aligned with the title.
 */
export function PageHeader({
  title,
  description,
  docs,
  actions,
}: {
  title: string;
  description?: string | undefined;
  /** Path under the documentation site, for example `tour/#explorer`. */
  docs?: string | undefined;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
      <div className="min-w-0">
        <h1 className="text-xl leading-9 font-semibold">{title}</h1>
        {(description !== undefined || docs !== undefined) && (
          <p className="max-w-3xl text-sm text-muted">
            {description}
            {description !== undefined && docs !== undefined && ' '}
            {docs !== undefined && <LearnMore path={docs} />}
          </p>
        )}
      </div>
      {actions !== undefined && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** A titled block of a screen; `actions` sit on the title line, aligned to its right. */
export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title: string;
  description?: string | undefined;
  actions?: ReactNode;
  children: ReactNode;
  className?: string | undefined;
}) {
  return (
    <section className={cn('mb-6', className)}>
      <div className="mb-2 flex min-h-7 flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions !== undefined && (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        )}
      </div>
      {description !== undefined && <p className="-mt-1 mb-2 text-sm text-muted">{description}</p>}
      {children}
    </section>
  );
}

/** A bordered surface. Cards in a grid stretch to the row height. */
export function Card({
  as: Tag = 'div',
  className,
  children,
  ...props
}: {
  as?: ElementType;
  className?: string | undefined;
  children: ReactNode;
} & Record<string, unknown>) {
  return (
    <Tag
      className={cn(
        'flex h-full min-w-0 flex-col rounded-md border border-line bg-surface p-4',
        className,
      )}
      {...props}
    >
      {children}
    </Tag>
  );
}

/**
 * A responsive grid of equal-height cards: as many columns of at least `minWidth` as fit, one
 * column on a phone.
 */
export function CardGrid({
  minWidth = '16rem',
  className,
  children,
  label,
}: {
  minWidth?: string;
  className?: string | undefined;
  children: ReactNode;
  label?: string;
}) {
  return (
    <div
      aria-label={label}
      role={label === undefined ? undefined : 'list'}
      className={cn('grid auto-rows-fr gap-4', className)}
      style={{ gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${minWidth}), 1fr))` }}
    >
      {children}
    </div>
  );
}

/**
 * A row of labelled controls and buttons. Controls align on their input boxes whatever their
 * hints; wrap buttons in `FormAction` so they line up with the inputs, not with the labels.
 */
export function FormRow({
  className,
  children,
  ...props
}: { className?: string | undefined; children: ReactNode } & Record<string, unknown>) {
  return (
    <div className={cn('flex flex-wrap items-start gap-3', className)} {...props}>
      {children}
    </div>
  );
}

/** Buttons of a `FormRow`, with a label-sized spacer so they sit on the line of the inputs. */
export function FormAction({
  className,
  children,
}: {
  className?: string | undefined;
  children: ReactNode;
}) {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <span aria-hidden className="invisible hidden text-xs font-medium sm:block">
        &nbsp;
      </span>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
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
      <p role="status" className="text-sm text-muted">
        {commonMessages.loading}
      </p>
    );
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-sm text-danger">
        {query.error.message}
      </p>
    );
  }
  return <>{children(query.data)}</>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-line bg-surface p-4 text-sm text-muted">{children}</p>
  );
}
