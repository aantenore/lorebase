import { lazy, Suspense } from 'react';
import type { GraphCanvasProps } from './graph-canvas';

const Canvas = lazy(() => import('./graph-canvas').then((m) => ({ default: m.GraphCanvas })));

/**
 * `GraphCanvas` loaded on demand, for screens whose main content is not the graph: the rest of
 * the screen shows at once and the renderer and layout code follow.
 */
export function LazyGraphCanvas(props: GraphCanvasProps) {
  return (
    <Suspense
      fallback={
        <p role="status" className="text-muted">
          Loading graph...
        </p>
      }
    >
      <Canvas {...props} />
    </Suspense>
  );
}
