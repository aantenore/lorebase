import { getRouteApi, useNavigate } from '@tanstack/react-router';
import { ProposalPage } from './proposal-page';

const route = getRouteApi('/$domain/review/$proposal');

/** One Proposal bound to its URL state; loaded lazily by the route. */
export function ProposalRoute() {
  const { proposal } = route.useParams();
  const { conflict, tab, item } = route.useSearch();
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
