import { getRouteApi } from '@tanstack/react-router';
import { ModelerPage } from './modeler-page';

const route = getRouteApi('/$domain/modeler');

/** The Modeler screen bound to its URL state; loaded lazily by the route. */
export function ModelerRoute() {
  const { tab, view } = route.useSearch();
  return <ModelerPage tab={tab ?? 'kinds'} view={view ?? 'overview'} />;
}
