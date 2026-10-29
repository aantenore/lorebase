import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useKeeperApi } from '@/api/provider';
import { useProposal, useProposalDiff } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { ItemsPanel } from '@/features/extraction/items-panel';
import { common as commonMessages } from '@/messages/common';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { review as reviewMessages } from '@/messages/review';
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

const text = reviewMessages.proposal;

function DiffSection({ domain, proposal }: { domain: string; proposal: string }) {
  const diff = useProposalDiff(domain, proposal);
  return (
    <Section title={text.diff.title}>
      <QueryBoundary query={diff}>
        {(data) =>
          Object.keys(data.summary).length === 0 ? (
            <EmptyState>{text.diff.empty}</EmptyState>
          ) : (
            <Table caption={text.diff.caption}>
              <THead>
                <TR>
                  <TH>{text.diff.columns.target}</TH>
                  <TH>{text.diff.columns.added}</TH>
                  <TH>{text.diff.columns.modified}</TH>
                  <TH>{text.diff.columns.removed}</TH>
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

/** Links to the open reconciliation candidates a `reconciliation` check names. */
function CheckCandidates({ domain, ids }: { domain: string; ids: string[] }) {
  const scopeSearch = useScopeSearch();
  return (
    <ul aria-label={reconcileMessages.check.candidates} className="flex flex-wrap gap-2">
      {ids.map((id) => (
        <li key={id}>
          <Link
            to="/$domain/reconcile"
            params={{ domain }}
            search={{ ...scopeSearch, candidate: id }}
            className="font-mono text-xs text-accent hover:underline"
          >
            {id}
          </Link>
        </li>
      ))}
    </ul>
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
        toast.notify(text.conflictsRemain(unresolvedOf(outcome.attempt).length), 'error');
      } else {
        toast.notify(text.merged(proposalId), 'success');
      }
      return refresh();
    },
  });
  const close = useMutation({
    mutationFn: () => api.proposals.close(scope.domain, proposalId),
    onSuccess: () => {
      toast.notify(text.closed(proposalId));
      return refresh();
    },
  });

  return (
    <QueryBoundary query={proposal}>
      {(detail) => {
        const overview = (
          <>
            <Section title={text.checks.title}>
              {detail.checks.length === 0 ? (
                <EmptyState>{text.checks.empty}</EmptyState>
              ) : (
                <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                  {detail.checks.map((check) => (
                    <li key={check.name} className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <Badge tone={CHECK_TONE[check.status]}>{check.status}</Badge>
                      <span>{check.name}</span>
                      {typeof check.message === 'string' && (
                        <span className="min-w-0 break-words text-muted">{check.message}</span>
                      )}
                      {(check.candidates ?? []).length > 0 && (
                        <CheckCandidates domain={scope.domain} ids={check.candidates ?? []} />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <Section title={text.reviews.title}>
              {detail.reviews.length === 0 ? (
                <EmptyState>{text.reviews.empty}</EmptyState>
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
              docs={text.docs}
              actions={
                <>
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
                        {text.merge}
                      </Button>
                      <Button
                        disabled={close.isPending}
                        onClick={() => {
                          close.mutate();
                        }}
                      >
                        {commonMessages.close}
                      </Button>
                    </>
                  )}
                </>
              }
            />
            <p className="-mt-3 mb-6 text-sm text-muted">
              {text.byline(detail.origin, detail.author, detail.source_ref, detail.target_ref)}{' '}
              <Link
                to="/$domain/review"
                params={{ domain: scope.domain }}
                search={scopeSearch}
                className="text-accent hover:underline"
              >
                {text.backToList}
              </Link>
            </p>

            {detail.origin === 'extraction' ? (
              <Tabs
                label={text.tabsLabel}
                value={tab}
                onChange={(id) => {
                  onTab(id === 'items' ? 'items' : 'overview');
                }}
                tabs={[
                  { id: 'overview', label: text.tabOverview, content: overview },
                  {
                    id: 'items',
                    label: text.tabItems,
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
