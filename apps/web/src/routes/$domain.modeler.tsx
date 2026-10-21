import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import {
  isModelerTab,
  isModelerView,
  type ModelerTab,
  type ModelerView,
} from '@/features/modeler/search';

export const Route = createFileRoute('/$domain/modeler')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): { tab?: ModelerTab | undefined; view?: ModelerView | undefined } => ({
    tab: isModelerTab(raw['tab']) ? raw['tab'] : undefined,
    view: isModelerView(raw['view']) ? raw['view'] : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/modeler/modeler-route'), 'ModelerRoute'),
});
