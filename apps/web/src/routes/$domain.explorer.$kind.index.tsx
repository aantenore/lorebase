import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { readText } from '@/app/scope';

export const Route = createFileRoute('/$domain/explorer/$kind/')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): {
    sort?: string | undefined;
    dir?: 'asc' | 'desc' | undefined;
    cursor?: string | undefined;
  } => ({
    sort: readText(raw['sort']),
    dir:
      raw['dir'] === 'desc'
        ? ('desc' as const)
        : raw['dir'] === 'asc'
          ? ('asc' as const)
          : undefined,
    cursor: readText(raw['cursor']),
  }),
  component: lazyRouteComponent(
    () => import('@/features/explorer/explorer-route'),
    'KindTableRoute',
  ),
});
