import type { InfiniteData, UseInfiniteQueryResult } from '@tanstack/react-query';
import type { Page } from '@/api/types';
import type { ReconciliationCandidate } from '@/api/reconciliation-types';
import { common as commonMessages } from '@/messages/common';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Stewards } from '@/features/admin/stewards';
import { Button } from '@/ui/button';
import { cn } from '@/ui/cn';
import { Badge, EmptyState } from '@/ui/layout';
import { candidateTitle, signalBadges } from './reconcile-state';
import { STATUS_TONE } from './status';

const text = reconcileMessages;

export interface CandidateListProps {
  query: UseInfiniteQueryResult<InfiniteData<Page<ReconciliationCandidate>>>;
  /** The loaded candidates after the signal filter, in display order. */
  candidates: ReconciliationCandidate[];
  active: string | null;
  selected: ReadonlySet<string>;
  onSelect: (ids: ReadonlySet<string>) => void;
  onOpen: (id: string) => void;
}

/** The queue: one row per candidate with its signals, a checkbox for bulk decisions. */
export function CandidateList({
  query,
  candidates,
  active,
  selected,
  onSelect,
  onOpen,
}: CandidateListProps) {
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
  if (candidates.length === 0) return <EmptyState>{text.list.empty}</EmptyState>;

  const open = candidates.filter((c) => c.status === 'open').map((c) => c.id);
  const allSelected = open.length > 0 && open.every((id) => selected.has(id));
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelect(next);
  };

  return (
    <div className="flex min-w-0 flex-col gap-2">
      {open.length > 0 && (
        <label className="flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => {
              onSelect(allSelected ? new Set() : new Set(open));
            }}
          />
          {text.list.selectAll}
        </label>
      )}
      <ul aria-label={text.list.label} className="flex flex-col gap-1">
        {candidates.map((candidate) => {
          const title = candidateTitle(candidate);
          const current = candidate.id === active;
          return (
            <li
              key={candidate.id}
              className={cn(
                'flex items-start gap-2 rounded-md border bg-surface p-2',
                current ? 'border-accent' : 'border-line',
              )}
            >
              <input
                type="checkbox"
                className="mt-1"
                aria-label={text.list.select(title)}
                disabled={candidate.status !== 'open'}
                checked={selected.has(candidate.id)}
                onChange={() => {
                  toggle(candidate.id);
                }}
              />
              <button
                type="button"
                aria-current={current ? 'true' : undefined}
                className="flex min-w-0 flex-1 flex-col items-start gap-1 text-left"
                onClick={() => {
                  onOpen(candidate.id);
                }}
              >
                <span className="text-sm font-medium break-words">{title}</span>
                <Stewards
                  roles={candidate.steward_roles}
                  assignees={candidate.assignees}
                  truncated={candidate.assignees_truncated}
                />
                <span className="flex flex-wrap gap-1">
                  {candidate.status !== 'open' && (
                    <Badge tone={STATUS_TONE[candidate.status]}>
                      {text.statuses[candidate.status]}
                    </Badge>
                  )}
                  {candidate.type !== 'duplicate' && (
                    <Badge>{text.list.claims(candidate.claim_count)}</Badge>
                  )}
                  {signalBadges(candidate.signals).map((badge) => (
                    <Badge key={badge.key} tone={badge.tone}>
                      {badge.label}
                    </Badge>
                  ))}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {query.hasNextPage && (
        <Button
          size="sm"
          disabled={query.isFetchingNextPage}
          onClick={() => {
            void query.fetchNextPage();
          }}
        >
          {text.list.loadMore}
        </Button>
      )}
    </div>
  );
}
