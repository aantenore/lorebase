import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';

/** Explore: questions to the optional guide service, streamed step by step. */
export const Route = createFileRoute('/$domain/explore')({
  component: lazyRouteComponent(() => import('@/features/explore/explore-page'), 'ExplorePage'),
});
