import { getRouteApi } from '@tanstack/react-router';
import { SettingsPage } from './settings-page';

const route = getRouteApi('/$domain/settings');

/** The Settings screen bound to its URL state; loaded lazily by the route. */
export function SettingsRoute() {
  const { section } = route.useSearch();
  return <SettingsPage section={section ?? 'general'} />;
}
