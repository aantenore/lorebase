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
import { common as commonMessages } from '@/messages/common';
import { review as reviewMessages } from '@/messages/review';
import { Button } from '@/ui/button';
import { Badge, EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { useToast } from '@/ui/toast';
import { BulkBar, FilterBar } from './attempt-toolbar';
import { ConflictList, listOrder, neighbor } from './conflict-list';
import { buildNames, partitionName, policyReason, specOf, type Names } from './conflict-meta';
import { ConflictResolver } from './conflict-resolver';
import {
  filterConflicts,
  NO_FILTER,
  resolutionOf,
  unresolvedOf,
  type ConflictFilter,
} from './conflict-state';
import { MergedSummary } from './merged-summary';

const text = reviewMessages.attempt;

interface RunReport {
  remaining: number;
  refused: number;
}

function Progress({ attempt }: { attempt: MergeAttempt }) {
  const total = attempt.conflicts.length;
  const resolved = total - unresolvedOf(attempt).length;
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <progress aria-label={text.progressLabel} max={total} value={resolved} className="h-2 w-40" />
      <span>{text.resolvedOf(resolved, total)}</span>
      {attempt.auto_resolutions.length > 0 && (
        <Badge tone="accent">{text.byPolicy(attempt.auto_resolutions.length)}</Badge>
      )}
    </div>
  );
}

function AutoResolutions({ attempt, names }: { attempt: MergeAttempt; names: Names }) {
  if (attempt.auto_resolutions.length === 0) return null;
  return (
    <details className="text-sm">
      <summary className="cursor-pointer">
        {text.resolvedByPolicy(attempt.auto_resolutions.length)}
      </summary>
      <ul className="mt-1 list-disc pl-5 text-muted">
        {attempt.auto_resolutions.map((auto) => (
          <li key={auto.fingerprint} className="break-words">
            {text.autoResolution(
              auto.kind,
              `${partitionName(auto.partition, names)}${auto.key === null ? '' : `/${auto.key}`}`,
              auto.choice,
              auto.resolver,
              policyReason(auto),
            )}
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
      {text.runStatus(report.remaining, report.refused)}
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
      toast.notify(text.resolutionSaved, 'success');
      return refresh();
    },
  });
  const saveMany = useMutation({
    mutationFn: (resolutions: Resolution[]) =>
      api.conflicts.resolveMany(domain, proposal.id, { resolutions }),
    onSuccess: (updated) => {
      toast.notify(text.resolutionsStored(updated.resolutions.length), 'success');
      return refresh();
    },
  });
  const apply = useMutation({
    mutationFn: () => api.proposals.merge(domain, proposal.id),
    onSuccess: (outcome) => {
      if (outcome.status === 'merged') {
        setReport(null);
        toast.notify(reviewMessages.proposal.merged(proposal.id), 'success');
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
              {text.nextOpen}
            </Button>
            <Button
              variant="primary"
              disabled={apply.isPending}
              onClick={() => {
                apply.mutate();
              }}
            >
              {text.apply}
            </Button>
          </div>
        )}
      </div>
      <AutoResolutions attempt={attempt} names={names} />
      <RunStatus report={report} />
      <FilterBar conflicts={attempt.conflicts} filter={filter} onChange={setFilter} />
      {!readOnly && <BulkBar open={open} disabled={saveMany.isPending} onApply={applyBulk} />}
      {filtered.length === 0 ? (
        <EmptyState>{text.noMatch}</EmptyState>
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 lg:grid-cols-[minmax(16rem,22rem)_minmax(0,1fr)]">
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
      <p className="text-xs text-muted">{text.keys}</p>
    </div>
  );
}

/**
 * The merge attempt of a Proposal: Conflicts grouped by type with filters, a resolver for the
 * selected one, bulk choices, and "Apply resolutions", which re-runs the merge and shows what
 * remains or the merged Revision.
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
    <Section title={text.title}>
      <QueryBoundary query={attempt}>
        {(data) =>
          data.conflicts.length === 0 && proposal.status !== 'merged' ? (
            <EmptyState>{text.clean}</EmptyState>
          ) : schema.isPending ? (
            <p role="status" className="text-muted">
              {commonMessages.loading}
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
