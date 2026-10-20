import { createFileRoute } from '@tanstack/react-router';
import { ModelerPage } from '@/features/modeler/modeler-page';
import { isModelerTab, type ModelerTab } from '@/features/modeler/search';

export const Route = createFileRoute('/$domain/modeler')({
  validateSearch: (raw: Record<string, unknown>): { tab?: ModelerTab | undefined } => ({
    tab: isModelerTab(raw['tab']) ? raw['tab'] : undefined,
  }),
  component: ModelerRoute,
});

function ModelerRoute() {
  const { tab } = Route.useSearch();
  return <ModelerPage tab={tab ?? 'kinds'} />;
}
