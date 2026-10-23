import { Link } from '@tanstack/react-router';
import { useRecords, useSchema } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { explorer as explorerMessages } from '@/messages/explorer';
import { EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { RecordsTable } from './records-table';

const t = explorerMessages.kind;

export interface KindTableSearch {
  sort?: string | undefined;
  dir?: 'asc' | 'desc' | undefined;
  cursor?: string | undefined;
}

/** Kind table: Records of one Kind, server-side sort and cursor paging. */
export function KindTablePage({ kind, search }: { kind: string; search: KindTableSearch }) {
  const scope = useScope();
  const config = useConfig();
  const scopeSearch = useScopeSearch();
  const schema = useSchema(scope);
  const sort =
    search.sort === undefined ? undefined : { attr: search.sort, dir: search.dir ?? 'asc' };
  const records = useRecords(scope, kind, {
    limit: config.pageSize,
    cursor: search.cursor ?? null,
    ...(sort === undefined ? {} : { order_by: [sort] }),
  });

  return (
    <>
      <PageHeader title={kind} description={t.description} />
      <QueryBoundary query={schema}>
        {(data) => {
          const definition = data.kinds.find((k) => k.api_name === kind);
          if (definition === undefined) {
            return <EmptyState>{t.missing(kind)}</EmptyState>;
          }
          return (
            <Section title={t.sectionTitle(definition.display_name)}>
              <QueryBoundary query={records}>
                {(page) => (
                  <>
                    {page.rows.length === 0 ? (
                      <EmptyState>{t.noRecords(kind)}</EmptyState>
                    ) : (
                      <RecordsTable
                        domain={scope.domain}
                        kind={definition}
                        rows={page.rows}
                        sort={sort}
                      />
                    )}
                    {page.next_cursor !== null && (
                      <p className="mt-3">
                        <Link
                          to="/$domain/explorer/$kind"
                          params={{ domain: scope.domain, kind }}
                          search={{ ...scopeSearch, ...search, cursor: page.next_cursor }}
                          className="text-accent hover:underline"
                        >
                          {t.nextPage}
                        </Link>
                      </p>
                    )}
                  </>
                )}
              </QueryBoundary>
            </Section>
          );
        }}
      </QueryBoundary>
    </>
  );
}
