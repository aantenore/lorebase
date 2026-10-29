import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { isStatusFilter, type StatusFilter } from '@/features/review/search';

export const Route = createFileRoute('/$domain/review/')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): {
    status?: StatusFilter | undefined;
    mine?: true | undefined;
    tab?: 'suggestions' | undefined;
  } => ({
    status: isStatusFilter(raw['status']) ? raw['status'] : undefined,
    mine: raw['mine'] === true || raw['mine'] === 'true' ? true : undefined,
    tab: raw['tab'] === 'suggestions' ? 'suggestions' : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/review/review-route'), 'ReviewRoute'),
});
