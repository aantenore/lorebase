import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { isCanvasView, type CanvasView } from '@/features/canvas/search';

export const Route = createFileRoute('/$domain/canvas')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): { view?: CanvasView | undefined; seed?: string | undefined } => ({
    view: isCanvasView(raw['view']) ? raw['view'] : undefined,
    seed: readText(raw['seed']),
  }),
  component: lazyRouteComponent(() => import('@/features/canvas/canvas-route'), 'CanvasRoute'),
});
