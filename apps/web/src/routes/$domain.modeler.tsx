import { createFileRoute } from '@tanstack/react-router';
import { ModelerPage } from '@/features/modeler/modeler-page';
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
  component: ModelerRoute,
});

function ModelerRoute() {
  const { tab, view } = Route.useSearch();
  return <ModelerPage tab={tab ?? 'kinds'} view={view ?? 'overview'} />;
}
