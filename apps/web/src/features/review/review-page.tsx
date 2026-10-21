import { Link, useNavigate } from '@tanstack/react-router';
import { useConflicts, useProposals } from '@/api/queries';
import type { Proposal, ProposalStatus } from '@/api/types';
import { useScope, useScopeSearch } from '@/app/scope';
import { PlannedScreens } from '@/features/planned-screens';
import { Select } from '@/ui/field';
import { Badge, EmptyState, PageHeader, QueryBoundary, type BadgeTone } from '@/ui/layout';
import { unresolvedOf } from './conflict-state';
import { isStatusFilter, type StatusFilter } from './search';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';

export const STATUS_TONE: Record<ProposalStatus, BadgeTone> = {
  open: 'accent',
  merged: 'success',
  closed: 'neutral',
};

/** Number of Conflicts of a Proposal, with how many are still unresolved. */
export function ConflictCount({ domain, proposal }: { domain: string; proposal: string }) {
  const conflicts = useConflicts(domain, proposal);
  if (conflicts.isPending) return <span className="text-muted">...</span>;
  if (conflicts.isError) return <span className="text-danger">error</span>;
  const total = conflicts.data.conflicts.length;
  const unresolved = unresolvedOf(conflicts.data).length;
  if (total === 0) return <Badge tone="success">0</Badge>;
  return (
    <Badge tone={unresolved > 0 ? 'danger' : 'success'}>
      {total} ({unresolved} unresolved)
    </Badge>
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
      </TD>
      <TD>{proposal.origin}</TD>
      <TD>
        {proposal.source_ref} to {proposal.target_ref}
      </TD>
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
export function ReviewPage({ status }: { status: StatusFilter }) {
  const scope = useScope();
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();
  const proposals = useProposals(scope.domain, status === 'all' ? undefined : { status });

  return (
    <>
      <PageHeader
        title="Review"
        description="Proposals waiting for review, with their Conflicts."
      />
      <div className="mb-4 w-48">
        <Select
          label="Status"
          value={status}
          options={[
            { value: 'open', label: 'Open' },
            { value: 'merged', label: 'Merged' },
            { value: 'closed', label: 'Closed' },
            { value: 'all', label: 'All' },
          ]}
          onChange={(event) => {
            const next = event.target.value;
            void navigate({
              to: '.',
              search: { ...scopeSearch, status: isStatusFilter(next) ? next : 'open' },
            });
          }}
        />
      </div>
      <QueryBoundary query={proposals}>
        {(page) =>
          page.items.length === 0 ? (
            <EmptyState>No Proposals match this filter.</EmptyState>
          ) : (
            <Table caption="Proposals">
              <THead>
                <TR>
                  <TH>Id</TH>
                  <TH>Title</TH>
                  <TH>Origin</TH>
                  <TH>Branches</TH>
                  <TH>Status</TH>
                  <TH>Conflicts</TH>
                </TR>
              </THead>
              <TBody>
                {page.items.map((proposal) => (
                  <ProposalRow key={proposal.id} domain={scope.domain} proposal={proposal} />
                ))}
              </TBody>
            </Table>
          )
        }
      </QueryBoundary>
      <div className="mt-6">
        <PlannedScreens
          area="Review"
          built={['Proposal list', 'Proposal detail', 'Extraction items', 'Conflict resolver']}
        />
      </div>
    </>
  );
}
