import { createFileRoute } from '@tanstack/react-router';
import { SettingsPage } from '@/features/settings/settings-page';
import { isSettingsSection, type SettingsSection } from '@/features/settings/search';

export const Route = createFileRoute('/$domain/settings')({
  validateSearch: (raw: Record<string, unknown>): { section?: SettingsSection | undefined } => ({
    section: isSettingsSection(raw['section']) ? raw['section'] : undefined,
  }),
  component: SettingsRoute,
});

function SettingsRoute() {
  const { section } = Route.useSearch();
  return <SettingsPage section={section ?? 'general'} />;
}
