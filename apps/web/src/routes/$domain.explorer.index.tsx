import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { readText } from '@/app/scope';

export const Route = createFileRoute('/$domain/explorer/')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): {
    q?: string | undefined;
    kind?: string | undefined;
    trait?: string | undefined;
    lens?: string | undefined;
  } => ({
    q: readText(raw['q']),
    kind: readText(raw['kind']),
    trait: readText(raw['trait']),
    lens: readText(raw['lens']),
  }),
  component: lazyRouteComponent(
    () => import('@/features/explorer/explorer-route'),
    'ExplorerRoute',
  ),
});
