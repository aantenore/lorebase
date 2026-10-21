import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { isSettingsSection, type SettingsSection } from '@/features/settings/search';

export const Route = createFileRoute('/$domain/settings')({
  validateSearch: (raw: Record<string, unknown>): { section?: SettingsSection | undefined } => ({
    section: isSettingsSection(raw['section']) ? raw['section'] : undefined,
  }),
  component: lazyRouteComponent(
    () => import('@/features/settings/settings-route'),
    'SettingsRoute',
  ),
});
