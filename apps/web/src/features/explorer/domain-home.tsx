import { Link } from '@tanstack/react-router';
import type { Schema } from '@/api/types';
import { useProposals, useRevisionLog } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { explorer as explorerMessages } from '@/messages/explorer';
import { Badge, Card, CardGrid, EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { shortTime } from '@/ui/time';

const t = explorerMessages.home;

/** Domain home: Kinds, recent Revisions, open Proposals. Source health is planned. */
export function DomainHome({ schema }: { schema: Schema }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const log = useRevisionLog(scope.domain, scope.branch, scope.head, { limit: 5 });
  const proposals = useProposals(scope.domain, { status: 'open' });

  return (
    <>
      <Section title={t.kinds}>
        {schema.kinds.length === 0 ? (
          <EmptyState>{t.noKinds}</EmptyState>
        ) : (
          <CardGrid label={t.kindsLabel}>
            {schema.kinds.map((kind) => (
              <Card key={kind.id} role="listitem" className="gap-1">
                <Link
                  to="/$domain/explorer/$kind"
                  params={{ domain: scope.domain, kind: kind.api_name }}
                  search={scopeSearch}
                  className="flex min-w-0 items-center gap-2 font-medium text-accent hover:underline"
                >
                  {kind.color !== null && (
                    <span
                      aria-hidden="true"
                      className="inline-block size-3 shrink-0 rounded-full"
                      style={{ backgroundColor: kind.color }}
                    />
                  )}
                  <span className="min-w-0 break-words">{kind.display_name}</span>
                </Link>
                <p className="text-xs text-muted">{kind.description}</p>
              </Card>
            ))}
          </CardGrid>
        )}
      </Section>

      <Section title={t.recentRevisions}>
        <QueryBoundary query={log}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>{t.noRevisions}</EmptyState>
            ) : (
              <ol className="divide-y divide-line rounded-md border border-line bg-surface">
                {page.items.map((revision) => (
                  <li key={revision.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 p-3">
                    <code className="font-mono text-xs">{revision.id.slice(0, 8)}</code>
                    <span className="min-w-0 flex-1 basis-48 break-words">{revision.message}</span>
                    <Badge>{revision.origin.type}</Badge>
                    <time
                      dateTime={revision.created_at}
                      className="text-xs whitespace-nowrap text-muted"
                    >
                      {shortTime(revision.created_at)}
                    </time>
                  </li>
                ))}
              </ol>
            )
          }
        </QueryBoundary>
      </Section>

      <Section title={t.openProposals}>
        <QueryBoundary query={proposals}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>{t.noProposals}</EmptyState>
            ) : (
              <ul className="divide-y divide-line rounded-md border border-line bg-surface">
                {page.items.map((p) => (
                  <li key={p.id} className="min-w-0 p-3">
                    <Link
                      to="/$domain/review/$proposal"
                      params={{ domain: scope.domain, proposal: p.id }}
                      search={scopeSearch}
                      className="break-words text-accent hover:underline"
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
