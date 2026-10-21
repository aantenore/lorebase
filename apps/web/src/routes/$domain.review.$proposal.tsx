import { createFileRoute, useNavigate } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { ProposalPage, type ProposalTab } from '@/features/review/proposal-page';

export const Route = createFileRoute('/$domain/review/$proposal')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): {
    conflict?: string | undefined;
    tab?: ProposalTab | undefined;
    item?: string | undefined;
  } => ({
    conflict: readText(raw['conflict']),
    tab: raw['tab'] === 'items' ? 'items' : undefined,
    item: readText(raw['item']),
  }),
  component: ProposalRoute,
});

function ProposalRoute() {
  const { proposal } = Route.useParams();
  const { conflict, tab, item } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <ProposalPage
      proposalId={proposal}
      conflict={conflict ?? null}
      tab={tab ?? (item === undefined ? 'overview' : 'items')}
      item={item ?? null}
      onSelectConflict={(fingerprint) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, conflict: fingerprint }),
          replace: true,
        });
      }}
      onTab={(next) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, tab: next === 'items' ? 'items' : undefined }),
          replace: true,
        });
      }}
      onSelectItem={(id) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, tab: 'items', item: id }),
          replace: true,
        });
      }}
    />
  );
}
