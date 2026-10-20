import { createFileRoute, Outlet } from '@tanstack/react-router';
import { AppShell } from '@/app/shell';
import { validateScopeSearch } from '@/app/scope';

export const Route = createFileRoute('/$domain')({
  validateSearch: validateScopeSearch,
  component: DomainLayout,
});

function DomainLayout() {
  const { domain } = Route.useParams();
  const search = Route.useSearch();
  return (
    <AppShell domain={domain} search={search}>
      <Outlet />
    </AppShell>
  );
}
