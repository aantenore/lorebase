import { createFileRoute } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { CanvasPage } from '@/features/canvas/canvas-page';
import { isCanvasView, type CanvasView } from '@/features/canvas/search';

export const Route = createFileRoute('/$domain/canvas')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): { view?: CanvasView | undefined; seed?: string | undefined } => ({
    view: isCanvasView(raw['view']) ? raw['view'] : undefined,
    seed: readText(raw['seed']),
  }),
  component: CanvasRoute,
});

function CanvasRoute() {
  const { view, seed } = Route.useSearch();
  return <CanvasPage view={view ?? 'graph'} seed={seed} />;
}
