import { createFileRoute, Navigate } from '@tanstack/react-router';
import { useDomains } from '@/api/queries';
import { EmptyState, QueryBoundary } from '@/ui/layout';

export const Route = createFileRoute('/')({
  component: DomainRedirect,
});

/** Opens the first Domain the caller can see. */
function DomainRedirect() {
  const domains = useDomains();
  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="mb-4 text-xl font-semibold">Lorebase</h1>
      <QueryBoundary query={domains}>
        {(page) => {
          const first = page.items[0];
          return first === undefined ? (
            <EmptyState>No Domains yet. Create one to begin.</EmptyState>
          ) : (
            <Navigate to="/$domain/explorer" params={{ domain: first.id }} replace />
          );
        }}
      </QueryBoundary>
    </main>
  );
}
