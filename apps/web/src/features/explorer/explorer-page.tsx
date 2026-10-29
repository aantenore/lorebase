import { useNavigate } from '@tanstack/react-router';
import { useState, type FormEvent } from 'react';
import { useRecords, useSchema, useSearch } from '@/api/queries';
import type { Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { common as commonMessages } from '@/messages/common';
import { explorer as explorerMessages } from '@/messages/explorer';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Badge, EmptyState, FormAction, FormRow, PageHeader, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { DomainHome } from './domain-home';
import { RecordsTable } from './records-table';

const ALL_KINDS = '';
const NO_LENS = '';
/** Prefix of a Trait in the Kind filter, whose values are otherwise Kind api names. */
const TRAIT = 'trait:';

const t = explorerMessages;

/** What the Explorer searches: the query, a Kind or a Trait, and a lens. */
export interface ExplorerFilter {
  q?: string | undefined;
  kind?: string | undefined;
  trait?: string | undefined;
  lens?: string | undefined;
}

function SearchForm({ schema, filter }: { schema: Schema; filter: ExplorerFilter }) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(filter.q ?? '');
  const scopeSearch = useScopeSearch();
  const lenses = schema.lenses ?? [];
  const lens = lenses.find((l) => l.api_name === filter.lens);
  const target =
    filter.trait !== undefined ? `${TRAIT}${filter.trait}` : (filter.kind ?? ALL_KINDS);

  const go = (next: { q?: string; target?: string; lens?: string }) => {
    const q = (next.q ?? draft).trim();
    const chosen = next.target ?? target;
    const nextLens = next.lens ?? filter.lens ?? NO_LENS;
    void navigate({
      to: '.',
      search: {
        ...scopeSearch,
        q: q === '' ? undefined : q,
        kind: chosen === ALL_KINDS || chosen.startsWith(TRAIT) ? undefined : chosen,
        trait: chosen.startsWith(TRAIT) ? chosen.slice(TRAIT.length) : undefined,
        lens: nextLens === NO_LENS ? undefined : nextLens,
      },
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    go({ q: draft });
  };

  return (
    <form onSubmit={submit} role="search" className="mb-6">
      <FormRow>
        <Input
          label={t.search.label}
          className="w-full sm:w-72"
          value={draft}
          placeholder={t.search.placeholder}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <Select
          label={t.search.kind}
          className="w-full sm:w-48"
          value={target}
          options={[
            { value: ALL_KINDS, label: commonMessages.allKinds },
            ...schema.kinds.map((k) => ({ value: k.api_name, label: k.display_name })),
            ...schema.traits.map((trait) => ({
              value: `${TRAIT}${trait.api_name}`,
              label: t.search.traitOption(trait.display_name || trait.api_name),
            })),
          ]}
          onChange={(event) => {
            go({ target: event.target.value });
          }}
        />
        {lenses.length > 0 && (
          <Select
            label={t.search.lens}
            className="w-full sm:w-48"
            value={filter.lens ?? NO_LENS}
            hint={
              lens === undefined || lens.description === '' ? t.search.lensHint : lens.description
            }
            options={[
              { value: NO_LENS, label: t.search.noLens },
              ...lenses.map((l) => ({ value: l.api_name, label: l.display_name })),
            ]}
            onChange={(event) => {
              go({ lens: event.target.value });
            }}
          />
        )}
        <FormAction>
          <Button type="submit" variant="primary">
            {commonMessages.search}
          </Button>
        </FormAction>
      </FormRow>
    </form>
  );
}

function SearchResults({ filter, q }: { filter: ExplorerFilter; q: string }) {
  const scope = useScope();
  const config = useConfig();
  const results = useSearch(scope, {
    q,
    ...(filter.kind === undefined ? {} : { kinds: [filter.kind] }),
    ...(filter.trait === undefined ? {} : { traits: [filter.trait] }),
    ...(filter.lens === undefined ? {} : { lens: filter.lens }),
    limit: config.searchLimit,
  });

  return (
    <QueryBoundary query={results}>
      {(data) =>
        data.results.length === 0 ? (
          <EmptyState>{t.search.noMatches(q)}</EmptyState>
        ) : (
          <Table caption={t.search.resultsCaption}>
            <THead>
              <TR>
                <TH>{t.search.columnRecord}</TH>
                <TH>{t.search.columnKind}</TH>
                <TH>{t.search.columnMatch}</TH>
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

  if (definition === undefined) return <EmptyState>{t.kind.missing(kind)}</EmptyState>;
  return (
    <QueryBoundary query={records}>
      {(page) =>
        page.rows.length === 0 ? (
          <EmptyState>{t.kind.noRecords(kind)}</EmptyState>
        ) : (
          <RecordsTable domain={scope.domain} kind={definition} rows={page.rows} />
        )
      }
    </QueryBoundary>
  );
}

/** Explorer entry: search box, Kind or Trait filter, lens, and results or the Domain home. */
export function ExplorerPage(filter: ExplorerFilter) {
  const scope = useScope();
  const schema = useSchema(scope);
  const { q, kind, trait } = filter;

  return (
    <>
      <PageHeader title={t.title} description={t.description} docs={t.docs} />
      <QueryBoundary query={schema}>
        {(data) => (
          <>
            <SearchForm schema={data} filter={filter} />
            {q !== undefined ? (
              <SearchResults filter={filter} q={q} />
            ) : trait !== undefined ? (
              <EmptyState>{t.search.traitNeedsQuery(trait)}</EmptyState>
            ) : kind !== undefined ? (
              <KindRecords schema={data} kind={kind} />
            ) : (
              <DomainHome schema={data} />
            )}
          </>
        )}
      </QueryBoundary>
    </>
  );
}
