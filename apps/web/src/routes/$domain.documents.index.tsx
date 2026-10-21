import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/$domain/documents/')({
  component: lazyRouteComponent(
    () => import('@/features/documents/documents-page'),
    'DocumentsPage',
  ),
});
