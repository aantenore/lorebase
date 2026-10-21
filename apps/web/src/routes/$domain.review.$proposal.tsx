import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import type { ProposalTab } from '@/features/review/proposal-page';

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
  component: lazyRouteComponent(() => import('@/features/review/proposal-route'), 'ProposalRoute'),
});
