import { useNavigate } from '@tanstack/react-router';
import { useState, type FormEvent } from 'react';
import { useRecords, useSchema, useSearch } from '@/api/queries';
import type { Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { PlannedScreens } from '@/features/planned-screens';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Badge, EmptyState, PageHeader, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { DomainHome } from './domain-home';
import { RecordsTable } from './records-table';

const ALL_KINDS = '';

function SearchForm({ schema, q, kind }: { schema: Schema; q: string; kind: string }) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(q);
  const scopeSearch = useScopeSearch();

  const go = (nextQ: string, nextKind: string) => {
    void navigate({
      to: '.',
      search: {
        ...scopeSearch,
        q: nextQ.trim() === '' ? undefined : nextQ.trim(),
        kind: nextKind === ALL_KINDS ? undefined : nextKind,
      },
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    go(draft, kind);
  };

  return (
    <form onSubmit={submit} role="search" className="mb-6 flex flex-wrap items-end gap-3">
      <Input
        label="Search Records"
        className="w-72"
        value={draft}
        placeholder="Name, code or text"
        onChange={(event) => {
          setDraft(event.target.value);
        }}
      />
      <Select
        label="Kind"
        className="w-48"
        value={kind}
        options={[
          { value: ALL_KINDS, label: 'All Kinds' },
          ...schema.kinds.map((k) => ({ value: k.api_name, label: k.display_name })),
        ]}
        onChange={(event) => {
          go(draft, event.target.value);
        }}
      />
      <Button type="submit" variant="primary">
        Search
      </Button>
    </form>
  );
}

function SearchResults({ q, kind }: { q: string; kind: string }) {
  const scope = useScope();
  const config = useConfig();
  const results = useSearch(scope, {
    q,
    ...(kind === ALL_KINDS ? {} : { kinds: [kind] }),
    limit: config.searchLimit,
  });

  return (
    <QueryBoundary query={results}>
      {(data) =>
        data.results.length === 0 ? (
          <EmptyState>No Records match "{q}".</EmptyState>
        ) : (
          <Table caption="Search results">
            <THead>
              <TR>
                <TH>Record</TH>
                <TH>Kind</TH>
                <TH>Match</TH>
              </TR>
            </THead>
            <TBody>
              {data.results.map((result) => (
                <TR key={result.ref}>
                  <TD>
                    <RecordLink domain={scope.domain} recordRef={result.ref}>
                      {result.title}
                    </RecordLink>
                  </TD>
                  <TD>
                    <Badge>{result.kind}</Badge>
                  </TD>
                  <TD className="text-muted">{result.snippet}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )
      }
    </QueryBoundary>
  );
}

function KindRecords({ schema, kind }: { schema: Schema; kind: string }) {
  const scope = useScope();
  const config = useConfig();
  const records = useRecords(scope, kind, { limit: config.pageSize });
  const definition = schema.kinds.find((k) => k.api_name === kind);

  if (definition === undefined)
    return <EmptyState>Kind {kind} does not exist on this Branch.</EmptyState>;
  return (
    <QueryBoundary query={records}>
      {(page) =>
        page.rows.length === 0 ? (
          <EmptyState>No {kind} Records.</EmptyState>
        ) : (
          <RecordsTable domain={scope.domain} kind={definition} rows={page.rows} />
        )
      }
    </QueryBoundary>
  );
}

/** Explorer entry: search box, Kind filter, and results or the Domain home. */
export function ExplorerPage({ q, kind }: { q: string | undefined; kind: string | undefined }) {
  const scope = useScope();
  const schema = useSchema(scope);

  return (
    <>
      <PageHeader
        title="Explorer"
        description={`Search and browse Records of ${scope.domainInfo.display_name} on ${scope.branch}.`}
      />
      <QueryBoundary query={schema}>
        {(data) => (
          <>
            <SearchForm schema={data} q={q ?? ''} kind={kind ?? ALL_KINDS} />
            {q !== undefined ? (
              <SearchResults q={q} kind={kind ?? ALL_KINDS} />
            ) : kind !== undefined ? (
              <KindRecords schema={data} kind={kind} />
            ) : (
              <DomainHome schema={data} />
            )}
          </>
        )}
      </QueryBoundary>
      <PlannedScreens
        area="Explorer"
        built={['Domain home', 'Search results', 'Kind table', 'Record page']}
      />
    </>
  );
}
