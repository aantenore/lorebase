import { Link, useNavigate } from '@tanstack/react-router';
import { useConflicts, useProposals } from '@/api/queries';
import type { Proposal, ProposalStatus } from '@/api/types';
import { useScope, useScopeSearch } from '@/app/scope';
import { common as commonMessages } from '@/messages/common';
import { review as reviewMessages } from '@/messages/review';
import { Stewards } from '@/features/admin/stewards';
import { Select } from '@/ui/field';
import { Badge, EmptyState, PageHeader, QueryBoundary, type BadgeTone } from '@/ui/layout';
import { unresolvedOf } from './conflict-state';
import { isStatusFilter, type ReviewTab, type StatusFilter } from './search';
import { SuggestionsPanel } from './suggestions-panel';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';

export const STATUS_TONE: Record<ProposalStatus, BadgeTone> = {
  open: 'accent',
  merged: 'success',
  closed: 'neutral',
};

const text = reviewMessages.list;
const assignedToMe = commonMessages.stewards.assignedToMe;

const STATUS_OPTIONS = (['open', 'merged', 'closed', 'all'] as const).map((value) => ({
  value,
  label: text.statusOptions[value],
}));

/** Number of Conflicts of a Proposal, with how many are still unresolved. */
export function ConflictCount({ domain, proposal }: { domain: string; proposal: string }) {
  const conflicts = useConflicts(domain, proposal);
  if (conflicts.isPending) return <span className="text-muted">{text.countPending}</span>;
  if (conflicts.isError) return <span className="text-danger">{text.countError}</span>;
  const total = conflicts.data.conflicts.length;
  const unresolved = unresolvedOf(conflicts.data).length;
  if (total === 0) return <Badge tone="success">0</Badge>;
  return (
    <Badge tone={unresolved > 0 ? 'danger' : 'success'}>{text.count(total, unresolved)}</Badge>
  );
}

function ProposalRow({ domain, proposal }: { domain: string; proposal: Proposal }) {
  const scopeSearch = useScopeSearch();
  return (
    <TR>
      <TD>#{proposal.id}</TD>
      <TD>
        <Link
          to="/$domain/review/$proposal"
          params={{ domain, proposal: proposal.id }}
          search={scopeSearch}
          className="text-accent hover:underline"
        >
          {proposal.title}
        </Link>
        <Stewards
          roles={proposal.steward_roles}
          assignees={proposal.assignees}
          truncated={proposal.assignees_truncated}
        />
      </TD>
      <TD>{proposal.origin}</TD>
      <TD>{text.branches(proposal.source_ref, proposal.target_ref)}</TD>
      <TD>
        <Badge tone={STATUS_TONE[proposal.status]}>{proposal.status}</Badge>
      </TD>
      <TD>
        <ConflictCount domain={domain} proposal={proposal.id} />
      </TD>
    </TR>
  );
}

/** Review: Proposals of the Domain with their Conflict counts. */
export function ReviewPage({
  status,
  mine,
  tab,
  onTab,
}: {
  status: StatusFilter;
  /** Only the Proposals assigned to the caller. */
  mine: boolean;
  tab: ReviewTab;
  onTab: (tab: ReviewTab) => void;
}) {
  const scope = useScope();
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();
  const proposals = useProposals(scope.domain, {
    ...(status === 'all' ? {} : { status }),
    ...(mine ? { assigned: 'me' as const } : {}),
  });

  return (
    <>
      <PageHeader title={text.title} description={text.description} docs={text.docs} />
      <Tabs
        label={text.tabsLabel}
        value={tab}
        onChange={(id) => {
          onTab(id === 'suggestions' ? 'suggestions' : 'proposals');
        }}
        tabs={[
          {
            id: 'proposals',
            label: text.tabProposals,
            content: (
              <>
                <div className="mb-4 flex flex-wrap items-end gap-4">
                  <div className="w-48 max-w-full">
                    <Select
                      label={text.statusLabel}
                      value={status}
                      options={STATUS_OPTIONS}
                      onChange={(event) => {
                        const next = event.target.value;
                        void navigate({
                          to: '.',
                          search: {
                            ...scopeSearch,
                            status: isStatusFilter(next) ? next : 'open',
                            mine: mine ? true : undefined,
                          },
                        });
                      }}
                    />
                  </div>
                  <label className="flex items-center gap-2 pb-2 text-sm">
                    <input
                      type="checkbox"
                      checked={mine}
                      onChange={(event) => {
                        void navigate({
                          to: '.',
                          search: {
                            ...scopeSearch,
                            status: status === 'open' ? undefined : status,
                            mine: event.target.checked ? true : undefined,
                          },
                        });
                      }}
                    />
                    {assignedToMe}
                  </label>
                </div>
                <QueryBoundary query={proposals}>
                  {(page) =>
                    page.items.length === 0 ? (
                      <EmptyState>{text.empty}</EmptyState>
                    ) : (
                      <Table caption={text.caption}>
                        <THead>
                          <TR>
                            <TH>{text.columns.id}</TH>
                            <TH>{text.columns.title}</TH>
                            <TH>{text.columns.origin}</TH>
                            <TH>{text.columns.branches}</TH>
                            <TH>{text.columns.status}</TH>
                            <TH>{text.columns.conflicts}</TH>
                          </TR>
                        </THead>
                        <TBody>
                          {page.items.map((proposal) => (
                            <ProposalRow
                              key={proposal.id}
                              domain={scope.domain}
                              proposal={proposal}
                            />
                          ))}
                        </TBody>
                      </Table>
                    )
                  }
                </QueryBoundary>
              </>
            ),
          },
          {
            id: 'suggestions',
            label: text.tabSuggestions,
            content: <SuggestionsPanel domain={scope.domain} />,
          },
        ]}
      />
    </>
  );
}
