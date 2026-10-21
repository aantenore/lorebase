import { getRouteApi } from '@tanstack/react-router';
import { CanvasPage } from './canvas-page';

const route = getRouteApi('/$domain/canvas');

/** The Canvas screen bound to its URL state; loaded lazily by the route. */
export function CanvasRoute() {
  const { view, seed } = route.useSearch();
  return <CanvasPage view={view ?? 'graph'} seed={seed} />;
}
