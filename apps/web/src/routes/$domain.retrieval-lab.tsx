import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { validateLabSearch } from '@/features/retrieval-lab/search';

export const Route = createFileRoute('/$domain/retrieval-lab')({
  validateSearch: validateLabSearch,
  component: lazyRouteComponent(
    () => import('@/features/retrieval-lab/retrieval-lab-route'),
    'RetrievalLabRoute',
  ),
});
