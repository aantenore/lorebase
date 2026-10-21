import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/$domain/sources')({
  component: lazyRouteComponent(() => import('@/features/sources/sources-page'), 'SourcesPage'),
});
