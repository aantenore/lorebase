import { getRouteApi, useNavigate } from '@tanstack/react-router';
import { ReconcilePage } from './reconcile-page';
import type { ReconcileSearch } from './search';

const route = getRouteApi('/$domain/reconcile');

/** The Reconciliation screen bound to its URL state; loaded lazily by the route. */
export function ReconcileRoute() {
  const search = route.useSearch();
  const navigate = useNavigate();
  return (
    <ReconcilePage
      search={search}
      onSearch={(change: Partial<ReconcileSearch>, replace = false) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, ...change }),
          replace,
        });
      }}
    />
  );
}
