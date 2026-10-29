import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { validateReconcileSearch } from '@/features/reconcile/search';

export const Route = createFileRoute('/$domain/reconcile')({
  validateSearch: validateReconcileSearch,
  component: lazyRouteComponent(
    () => import('@/features/reconcile/reconcile-route'),
    'ReconcileRoute',
  ),
});
