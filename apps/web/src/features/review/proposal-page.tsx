import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { isApiError } from '@/api';
import { useKeeperApi } from '@/api/provider';
import { useConflicts, useProposal, useProposalDiff } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { Button } from '@/ui/button';
import {
  Badge,
  EmptyState,
  PageHeader,
  PlaceholderSection,
  QueryBoundary,
  Section,
} from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { useToast } from '@/ui/toast';
import { resolutionOf, unresolvedOf } from './conflict-state';
import { STATUS_TONE } from './review-page';

const CHECK_TONE = { passed: 'success', failed: 'danger', pending: 'warning' } as const;

function ConflictsSection({ domain, proposal }: { domain: string; proposal: string }) {
  const conflicts = useConflicts(domain, proposal);
  return (
    <Section title="Conflicts">
      <QueryBoundary query={conflicts}>
        {(attempt) =>
          attempt.conflicts.length === 0 ? (
            <EmptyState>No Conflicts.</EmptyState>
          ) : (
            <Table caption="Conflicts">
              <THead>
                <TR>
                  <TH>Code</TH>
                  <TH>Element</TH>
                  <TH>Members</TH>
                  <TH>Resolution</TH>
                </TR>
              </THead>
              <TBody>
                {attempt.conflicts.map((c) => (
                  <TR key={c.fingerprint}>
                    <TD>
                      <Badge tone="danger">{c.kind}</Badge>
                    </TD>
                    <TD>
                      {c.partition}/{c.key ?? c.attribute ?? ''}
                    </TD>
                    <TD>{c.count}</TD>
                    <TD>{resolutionOf(attempt, c)?.choice ?? 'Unresolved'}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )
        }
      </QueryBoundary>
    </Section>
  );
}

function DiffSection({ domain, proposal }: { domain: string; proposal: string }) {
  const diff = useProposalDiff(domain, proposal);
  return (
    <Section title="Data diff by Kind">
      <QueryBoundary query={diff}>
        {(data) =>
          Object.keys(data.summary).length === 0 ? (
            <EmptyState>No differences.</EmptyState>
          ) : (
            <Table caption="Changes by Kind">
              <THead>
                <TR>
                  <TH>Kind or Relation</TH>
                  <TH>Added</TH>
                  <TH>Modified</TH>
                  <TH>Removed</TH>
                </TR>
              </THead>
              <TBody>
                {Object.entries(data.summary).map(([kind, counts]) => (
                  <TR key={kind}>
                    <TD>{kind}</TD>
                    <TD>{counts.added}</TD>
                    <TD>{counts.modified}</TD>
                    <TD>{counts.removed}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )
        }
      </QueryBoundary>
    </Section>
  );
}

/** Proposal detail: summary, checks, reviews, Conflicts, diff, merge and close. */
export function ProposalPage({ proposalId }: { proposalId: string }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const proposal = useProposal(scope.domain, proposalId);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper'] });
  const onError = (error: Error) => {
    toast.notify(isApiError(error) ? error.message : 'The request failed.', 'error');
  };
  const merge = useMutation({
    mutationFn: () => api.proposals.merge(scope.domain, proposalId),
    onSuccess: (outcome) => {
      if (outcome.status === 'conflicts') {
        const open = unresolvedOf(outcome.attempt).length;
        toast.notify(
          `${open} ${open === 1 ? 'Conflict still needs' : 'Conflicts still need'} a resolution.`,
          'error',
        );
      } else {
        toast.notify(`Proposal #${proposalId} merged.`, 'success');
      }
      return refresh();
    },
    onError,
  });
  const close = useMutation({
    mutationFn: () => api.proposals.close(scope.domain, proposalId),
    onSuccess: () => {
      toast.notify(`Proposal #${proposalId} closed.`);
      return refresh();
    },
    onError,
  });

  return (
    <QueryBoundary query={proposal}>
      {(detail) => (
        <>
          <PageHeader
            title={`#${detail.id} ${detail.title}`}
            description={detail.description}
            actions={
              <div className="flex items-center gap-2">
                <Badge tone={STATUS_TONE[detail.status]}>{detail.status}</Badge>
                {detail.status === 'open' && (
                  <>
                    <Button
                      variant="primary"
                      disabled={merge.isPending}
                      onClick={() => {
                        merge.mutate();
                      }}
                    >
                      Merge
                    </Button>
                    <Button
                      disabled={close.isPending}
                      onClick={() => {
                        close.mutate();
                      }}
                    >
                      Close
                    </Button>
                  </>
                )}
              </div>
            }
          />
          <p className="mb-4 text-sm text-muted">
            {detail.origin} Proposal by {detail.author}: {detail.source_ref} into{' '}
            {detail.target_ref}.{' '}
            <Link
              to="/$domain/review"
              params={{ domain: scope.domain }}
              search={scopeSearch}
              className="text-accent hover:underline"
            >
              Back to the list
            </Link>
          </p>

          <Section title="Checks">
            {detail.checks.length === 0 ? (
              <EmptyState>No checks have run.</EmptyState>
            ) : (
              <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                {detail.checks.map((check) => (
                  <li key={check.name} className="flex flex-wrap items-center gap-2 px-3 py-2">
                    <Badge tone={CHECK_TONE[check.status]}>{check.status}</Badge>
                    <span>{check.name}</span>
                    {check.message !== undefined && (
                      <span className="text-muted">{check.message}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Reviews">
            {detail.reviews.length === 0 ? (
              <EmptyState>No reviews yet.</EmptyState>
            ) : (
              <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                {detail.reviews.map((review) => (
                  <li key={`${review.reviewer}-${review.at}`} className="px-3 py-2">
                    <Badge>{review.verdict}</Badge>{' '}
                    <span className="font-medium">{review.reviewer}</span>{' '}
                    <span className="text-muted">{review.comment}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <ConflictsSection domain={scope.domain} proposal={detail.id} />
          <DiffSection domain={scope.domain} proposal={detail.id} />

          <div className="grid gap-3 sm:grid-cols-2">
            <PlaceholderSection
              area="Review"
              screen="Extraction items"
              content="Item list with confidence, match status and evidence; accept or reject per item."
            />
            <PlaceholderSection
              area="Review"
              screen="Conflict resolver"
              content="Base, ours and theirs panes with choices and a custom value editor."
            />
          </div>
        </>
      )}
    </QueryBoundary>
  );
}
