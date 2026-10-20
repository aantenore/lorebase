import { createFileRoute, useNavigate } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { ProposalPage } from '@/features/review/proposal-page';

export const Route = createFileRoute('/$domain/review/$proposal')({
  validateSearch: (raw: Record<string, unknown>): { conflict?: string | undefined } => ({
    conflict: readText(raw['conflict']),
  }),
  component: ProposalRoute,
});

function ProposalRoute() {
  const { proposal } = Route.useParams();
  const { conflict } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <ProposalPage
      proposalId={proposal}
      conflict={conflict ?? null}
      onSelectConflict={(fingerprint) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, conflict: fingerprint }),
          replace: true,
        });
      }}
    />
  );
}
