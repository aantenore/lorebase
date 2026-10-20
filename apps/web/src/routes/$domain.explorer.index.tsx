import { createFileRoute } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { ExplorerPage } from '@/features/explorer/explorer-page';

export const Route = createFileRoute('/$domain/explorer/')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): { q?: string | undefined; kind?: string | undefined } => ({
    q: readText(raw['q']),
    kind: readText(raw['kind']),
  }),
  component: ExplorerRoute,
});

function ExplorerRoute() {
  const { q, kind } = Route.useSearch();
  return <ExplorerPage q={q} kind={kind} />;
}
