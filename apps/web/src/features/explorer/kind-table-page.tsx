import { Link } from '@tanstack/react-router';
import { useRecords, useSchema } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { RecordsTable } from './records-table';

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
      <PageHeader title={kind} description="Records of this Kind." />
      <QueryBoundary query={schema}>
        {(data) => {
          const definition = data.kinds.find((k) => k.api_name === kind);
          if (definition === undefined) {
            return (
              <EmptyState>
                Kind {kind} does not exist on {scope.branch}.
              </EmptyState>
            );
          }
          return (
            <Section title={`${definition.display_name} Records`}>
              <QueryBoundary query={records}>
                {(page) => (
                  <>
                    {page.rows.length === 0 ? (
                      <EmptyState>No {kind} Records.</EmptyState>
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
                          Next page
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
