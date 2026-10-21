import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/$domain/explorer/$kind/$key')({
  component: lazyRouteComponent(() => import('@/features/explorer/explorer-route'), 'RecordRoute'),
});
