import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import { useBranchSchema, useConflicts } from '@/api/queries';
import type {
  Conflict,
  MergeAttempt,
  ProposalDetail,
  Resolution,
  ResolutionChoice,
  Schema,
} from '@/api/types';
import { Button } from '@/ui/button';
import { Badge, EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { useToast } from '@/ui/toast';
import { BulkBar, FilterBar } from './attempt-toolbar';
import { ConflictList, listOrder, neighbor } from './conflict-list';
import { buildNames, partitionName, specOf, type Names } from './conflict-meta';
import { ConflictResolver } from './conflict-resolver';
import {
  filterConflicts,
  NO_FILTER,
  resolutionOf,
  unresolvedOf,
  type ConflictFilter,
} from './conflict-state';
import { MergedSummary } from './merged-summary';

interface RunReport {
  remaining: number;
  refused: number;
}

function Progress({ attempt }: { attempt: MergeAttempt }) {
  const total = attempt.conflicts.length;
  const resolved = total - unresolvedOf(attempt).length;
  return (
    <div className="flex items-center gap-3 text-sm">
      <progress aria-label="Resolved Conflicts" max={total} value={resolved} className="h-2 w-40" />
      <span>
        {resolved} of {total} resolved
      </span>
      {attempt.auto_resolutions.length > 0 && (
        <Badge tone="accent">{attempt.auto_resolutions.length} by policy</Badge>
      )}
    </div>
  );
}

function AutoResolutions({ attempt, names }: { attempt: MergeAttempt; names: Names }) {
  if (attempt.auto_resolutions.length === 0) return null;
  return (
    <details className="text-sm">
      <summary className="cursor-pointer">
        Resolved by policy ({attempt.auto_resolutions.length})
      </summary>
      <ul className="mt-1 list-disc pl-5 text-muted">
        {attempt.auto_resolutions.map((auto) => (
          <li key={auto.fingerprint}>
            {auto.kind} {partitionName(auto.partition, names)}
            {auto.key === null ? '' : `/${auto.key}`}: {auto.choice} by {auto.resolver}
          </li>
        ))}
      </ul>
    </details>
  );
}

function RunStatus({ report }: { report: RunReport | null }) {
  if (report === null) return null;
  return (
    <p role="status" className="rounded-md border border-line bg-surface p-2 text-sm">
      Merge run: {report.remaining}{' '}
      {report.remaining === 1 ? 'Conflict remains' : 'Conflicts remain'}
      {report.refused > 0
        ? `, ${report.refused} stored resolution(s) refused on re-validation`
        : ''}
      .
    </p>
  );
}

interface BodyProps {
  domain: string;
  proposal: ProposalDetail;
  attempt: MergeAttempt;
  schema: Schema | undefined;
  selected: string | null;
  onSelect: (fingerprint: string) => void;
}

function AttemptBody({ domain, proposal, attempt, schema, selected, onSelect }: BodyProps) {
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ConflictFilter>(NO_FILTER);
  const [report, setReport] = useState<RunReport | null>(null);

  const names = buildNames(schema);
  const filtered = filterConflicts(attempt, filter, names);
  const order = listOrder(filtered);
  const current = order.find((c) => c.fingerprint === selected) ?? order[0];
  const readOnly = proposal.status !== 'open';

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper'] });

  const save = useMutation({
    mutationFn: (resolution: Resolution) => api.conflicts.resolve(domain, proposal.id, resolution),
    onSuccess: () => {
      toast.notify('Resolution saved.', 'success');
      return refresh();
    },
  });
  const saveMany = useMutation({
    mutationFn: (resolutions: Resolution[]) =>
      api.conflicts.resolveMany(domain, proposal.id, { resolutions }),
    onSuccess: (updated) => {
      toast.notify(`${updated.resolutions.length} resolutions stored.`, 'success');
      return refresh();
    },
  });
  const apply = useMutation({
    mutationFn: () => api.proposals.merge(domain, proposal.id),
    onSuccess: (outcome) => {
      if (outcome.status === 'merged') {
        setReport(null);
        toast.notify(`Proposal #${proposal.id} merged.`, 'success');
      } else {
        const refused = outcome.attempt.conflicts.filter(
          (c) => typeof c.details['rejected'] === 'string',
        ).length;
        setReport({ remaining: unresolvedOf(outcome.attempt).length, refused });
      }
      return refresh();
    },
  });

  const open = filtered.filter((c) => resolutionOf(attempt, c) === undefined);
  const nextOpen = (): Conflict | undefined => {
    const openOrder = listOrder(unresolvedOf(attempt));
    return neighbor(openOrder, current?.fingerprint ?? null, 1);
  };

  const applyBulk = (choice: ResolutionChoice, targets: Conflict[]) => {
    saveMany.mutate(targets.map((c) => ({ fingerprint: c.fingerprint, choice })));
  };

  return (
    <div className="flex flex-col gap-4">
      {proposal.status === 'merged' && proposal.merged_revision !== null && (
        <MergedSummary
          domain={domain}
          revisionId={proposal.merged_revision}
          attempt={attempt}
          names={names}
        />
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Progress attempt={attempt} />
        {!readOnly && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              disabled={unresolvedOf(attempt).length === 0}
              onClick={() => {
                const next = nextOpen();
                if (next !== undefined) {
                  setFilter(NO_FILTER);
                  onSelect(next.fingerprint);
                }
              }}
            >
              Next open Conflict
            </Button>
            <Button
              variant="primary"
              disabled={apply.isPending}
              onClick={() => {
                apply.mutate();
              }}
            >
              Apply resolutions
            </Button>
          </div>
        )}
      </div>
      <AutoResolutions attempt={attempt} names={names} />
      <RunStatus report={report} />
      <FilterBar conflicts={attempt.conflicts} filter={filter} onChange={setFilter} />
      {!readOnly && <BulkBar open={open} disabled={saveMany.isPending} onApply={applyBulk} />}
      {filtered.length === 0 ? (
        <EmptyState>No Conflict matches the filter.</EmptyState>
      ) : (
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(16rem,22rem)_1fr]">
          <ConflictList
            conflicts={filtered}
            resolutions={attempt.resolutions}
            selected={current?.fingerprint ?? null}
            names={names}
            onSelect={onSelect}
          />
          {current !== undefined && (
            <ConflictResolver
              key={current.fingerprint}
              conflict={current}
              resolution={resolutionOf(attempt, current)}
              names={names}
              spec={specOf(current, schema)}
              readOnly={readOnly}
              saving={save.isPending}
              onSave={(resolution) => {
                save.mutate(resolution);
              }}
            />
          )}
        </div>
      )}
      <p className="text-xs text-muted">
        Keys in the list: arrow keys or j and k move between Conflicts, Home and End jump to the
        first and last.
      </p>
    </div>
  );
}

/**
 * The merge attempt of a Proposal (design 12.3, Conflict resolver): Conflicts grouped by
 * kind with filters, a resolver for the selected one, bulk choices, and "Apply resolutions",
 * which re-runs the merge and shows what remains or the merged Revision.
 */
export function MergeAttemptSection({
  domain,
  proposal,
  selected,
  onSelect,
}: {
  domain: string;
  proposal: ProposalDetail;
  selected: string | null;
  onSelect: (fingerprint: string) => void;
}) {
  const attempt = useConflicts(domain, proposal.id);
  const schema = useBranchSchema(domain, proposal.source_ref);
  return (
    <Section title="Merge attempt">
      <QueryBoundary query={attempt}>
        {(data) =>
          data.conflicts.length === 0 && proposal.status !== 'merged' ? (
            <EmptyState>No Conflicts. The merge applies cleanly.</EmptyState>
          ) : schema.isPending ? (
            <p role="status" className="text-muted">
              Loading...
            </p>
          ) : (
            <AttemptBody
              domain={domain}
              proposal={proposal}
              attempt={data}
              schema={schema.data}
              selected={selected}
              onSelect={onSelect}
            />
          )
        }
      </QueryBoundary>
    </Section>
  );
}
