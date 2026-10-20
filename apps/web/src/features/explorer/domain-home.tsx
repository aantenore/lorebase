import { Link } from '@tanstack/react-router';
import type { Schema } from '@/api/types';
import { useProposals, useRevisionLog } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { Badge, EmptyState, QueryBoundary, Section } from '@/ui/layout';

/** Domain home: Kinds, recent Revisions, open Proposals. Source health is planned. */
export function DomainHome({ schema }: { schema: Schema }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const log = useRevisionLog(scope.domain, scope.branch, scope.head, { limit: 5 });
  const proposals = useProposals(scope.domain, { status: 'open' });

  return (
    <>
      <Section title="Kinds">
        {schema.kinds.length === 0 ? (
          <EmptyState>This Branch has no Kinds yet.</EmptyState>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {schema.kinds.map((kind) => (
              <li key={kind.id} className="rounded-md border border-line bg-surface p-3">
                <Link
                  to="/$domain/explorer/$kind"
                  params={{ domain: scope.domain, kind: kind.api_name }}
                  search={scopeSearch}
                  className="flex items-center gap-2 font-medium text-accent hover:underline"
                >
                  {kind.color !== null && (
                    <span
                      aria-hidden="true"
                      className="inline-block size-3 rounded-full"
                      style={{ backgroundColor: kind.color }}
                    />
                  )}
                  {kind.display_name}
                </Link>
                <p className="mt-1 text-xs text-muted">{kind.description}</p>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Recent Revisions">
        <QueryBoundary query={log}>
          {(page) => (
            <ol className="divide-y divide-line rounded-md border border-line bg-surface">
              {page.items.map((revision) => (
                <li key={revision.id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                  <code className="font-mono text-xs">{revision.id.slice(0, 8)}</code>
                  <span>{revision.message}</span>
                  <Badge>{revision.origin.type}</Badge>
                  <span className="ml-auto text-xs text-muted">{revision.created_at}</span>
                </li>
              ))}
            </ol>
          )}
        </QueryBoundary>
      </Section>

      <Section title="Open Proposals">
        <QueryBoundary query={proposals}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>No open Proposals.</EmptyState>
            ) : (
              <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                {page.items.map((p) => (
                  <li key={p.id} className="px-3 py-2">
                    <Link
                      to="/$domain/review/$proposal"
                      params={{ domain: scope.domain, proposal: p.id }}
                      search={scopeSearch}
                      className="text-accent hover:underline"
                    >
                      #{p.id} {p.title}
                    </Link>
                  </li>
                ))}
              </ul>
            )
          }
        </QueryBoundary>
      </Section>
    </>
  );
}
