import { createFileRoute } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { KindTablePage } from '@/features/explorer/kind-table-page';

export const Route = createFileRoute('/$domain/explorer/$kind/')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): {
    sort?: string | undefined;
    dir?: 'asc' | 'desc' | undefined;
    cursor?: string | undefined;
  } => ({
    sort: readText(raw['sort']),
    dir:
      raw['dir'] === 'desc'
        ? ('desc' as const)
        : raw['dir'] === 'asc'
          ? ('asc' as const)
          : undefined,
    cursor: readText(raw['cursor']),
  }),
  component: KindTableRoute,
});

function KindTableRoute() {
  const { kind } = Route.useParams();
  const { sort, dir, cursor } = Route.useSearch();
  return <KindTablePage kind={kind} search={{ sort, dir, cursor }} />;
}
