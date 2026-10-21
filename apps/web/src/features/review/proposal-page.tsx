import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useKeeperApi } from '@/api/provider';
import { useProposal, useProposalDiff } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { ItemsPanel } from '@/features/extraction/items-panel';
import { Button } from '@/ui/button';
import { Badge, EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { Tabs } from '@/ui/tabs';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { useToast } from '@/ui/toast';
import { MergeAttemptSection } from './merge-attempt';
import { ReviewForm } from './review-form';
import { unresolvedOf } from './conflict-state';
import { STATUS_TONE } from './review-page';

const CHECK_TONE = { passed: 'success', failed: 'danger', pending: 'warning' } as const;

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

export type ProposalTab = 'overview' | 'items';

/** Proposal detail: summary, checks, reviews, Conflicts, diff, merge and close. */
export function ProposalPage({
  proposalId,
  conflict = null,
  tab = 'overview',
  item = null,
  onSelectConflict,
  onTab = () => undefined,
  onSelectItem = () => undefined,
}: {
  proposalId: string;
  /** Fingerprint of the Conflict open in the resolver. */
  conflict?: string | null;
  /** `items` shows the extraction items of an extraction Proposal. */
  tab?: ProposalTab;
  /** Id of the extraction item selected in the Items tab. */
  item?: string | null;
  onSelectConflict: (fingerprint: string) => void;
  onTab?: (tab: ProposalTab) => void;
  onSelectItem?: (id: string) => void;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const proposal = useProposal(scope.domain, proposalId);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper'] });
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
  });
  const close = useMutation({
    mutationFn: () => api.proposals.close(scope.domain, proposalId),
    onSuccess: () => {
      toast.notify(`Proposal #${proposalId} closed.`);
      return refresh();
    },
  });

  return (
    <QueryBoundary query={proposal}>
      {(detail) => {
        const overview = (
          <>
            <Section title="Checks">
              {detail.checks.length === 0 ? (
                <EmptyState>No checks have run.</EmptyState>
              ) : (
                <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                  {detail.checks.map((check) => (
                    <li key={check.name} className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <Badge tone={CHECK_TONE[check.status]}>{check.status}</Badge>
                      <span>{check.name}</span>
                      {typeof check.message === 'string' && (
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
                    <li key={review.id} className="px-3 py-2">
                      <Badge>{review.verdict}</Badge>{' '}
                      <span className="font-medium">{review.reviewer}</span>{' '}
                      <span className="text-muted">{review.comment}</span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3">
                <ReviewForm domain={scope.domain} proposal={detail} />
              </div>
            </Section>

            <MergeAttemptSection
              domain={scope.domain}
              proposal={detail}
              selected={conflict}
              onSelect={onSelectConflict}
            />
            <DiffSection domain={scope.domain} proposal={detail.id} />
          </>
        );
        return (
          <>
            <PageHeader
              title={`#${detail.id} ${detail.title}`}
              description={detail.description ?? undefined}
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

            {detail.origin === 'extraction' ? (
              <Tabs
                label="Proposal sections"
                value={tab}
                onChange={(id) => {
                  onTab(id === 'items' ? 'items' : 'overview');
                }}
                tabs={[
                  { id: 'overview', label: 'Overview', content: overview },
                  {
                    id: 'items',
                    label: 'Items',
                    content: (
                      <ItemsPanel
                        domain={scope.domain}
                        proposal={detail}
                        selected={item}
                        onSelect={onSelectItem}
                      />
                    ),
                  },
                ]}
              />
            ) : (
              overview
            )}
          </>
        );
      }}
    </QueryBoundary>
  );
}
