import { useMutation } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import { useCandidateCounts, useCandidates, useSchema } from '@/api/queries';
import type { CandidateFilter, CandidateType } from '@/api/reconciliation-types';
import { useScope } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { EmptyState, FormRow, PageHeader } from '@/ui/layout';
import { Tabs } from '@/ui/tabs';
import { useToast } from '@/ui/toast';
import { BulkBar } from './bulk-bar';
import { CandidateDetail } from './candidate-detail';
import { CandidateList } from './candidate-list';
import { countLabel, rankCandidates, signalNames } from './reconcile-state';
import {
  CANDIDATE_TYPES,
  DEFAULT_TYPE,
  STATUS_FILTERS,
  type ReconcileSearch,
  type StatusFilter,
} from './search';

const text = reconcileMessages;

export interface ReconcilePageProps {
  search: ReconcileSearch;
  onSearch: (change: Partial<ReconcileSearch>, replace?: boolean) => void;
}

function Filters({
  search,
  status,
  signals,
  onSearch,
}: {
  search: ReconcileSearch;
  status: StatusFilter;
  signals: string[];
  onSearch: ReconcilePageProps['onSearch'];
}) {
  const scope = useScope();
  const schema = useSchema(scope);
  const kinds = schema.data?.kinds ?? [];
  const kind = kinds.find((k) => k.api_name === search.kind);
  const attributes = Object.values(kind?.attributes ?? {});
  // The text of the source filter is applied on Enter or when the field is left.
  const [source, setSource] = useState(search.source ?? '');
  const applySource = () => {
    const next = source.trim();
    if (next !== (search.source ?? '')) {
      onSearch({ source: next === '' ? undefined : next, candidate: undefined });
    }
  };
  const known = signals.includes(search.signal ?? '') || search.signal === undefined;
  return (
    <FormRow role="group" aria-label={text.filters.label} className="mb-4">
      <Select
        className="w-36"
        label={text.filters.status}
        value={status}
        options={STATUS_FILTERS.map((value) => ({ value, label: text.statuses[value] }))}
        onChange={(event) => {
          const next = STATUS_FILTERS.find((s) => s === event.target.value) ?? 'open';
          onSearch({ status: next === 'open' ? undefined : next, candidate: undefined });
        }}
      />
      <Select
        className="w-44"
        label={text.filters.kind}
        value={search.kind ?? ''}
        options={[
          { value: '', label: text.filters.allKinds },
          ...kinds.map((k) => ({ value: k.api_name, label: k.display_name })),
        ]}
        onChange={(event) => {
          const next = event.target.value;
          onSearch({
            kind: next === '' ? undefined : next,
            attribute: undefined,
            candidate: undefined,
          });
        }}
      />
      <Select
        className="w-44"
        label={text.filters.attribute}
        value={search.attribute ?? ''}
        disabled={kind === undefined}
        {...(kind === undefined ? { hint: text.filters.attributeHint } : {})}
        options={[
          { value: '', label: text.filters.allAttributes },
          ...attributes.map((a) => ({ value: a.api_name, label: a.display_name })),
        ]}
        onChange={(event) => {
          const next = event.target.value;
          onSearch({ attribute: next === '' ? undefined : next, candidate: undefined });
        }}
      />
      <Input
        className="w-52"
        label={text.filters.source}
        placeholder={text.filters.sourcePlaceholder}
        value={source}
        onChange={(event) => {
          setSource(event.target.value);
        }}
        onBlur={applySource}
        onKeyDown={(event) => {
          if (event.key === 'Enter') applySource();
        }}
      />
      <Select
        className="w-40"
        label={text.filters.signal}
        hint={text.filters.signalHint}
        value={search.signal ?? ''}
        options={[
          { value: '', label: text.filters.allSignals },
          ...(known || search.signal === undefined
            ? []
            : [{ value: search.signal, label: search.signal }]),
          ...signals.map((value) => ({ value, label: value })),
        ]}
        onChange={(event) => {
          const next = event.target.value;
          onSearch({ signal: next === '' ? undefined : next, candidate: undefined });
        }}
      />
    </FormRow>
  );
}

function Queue({ search, onSearch }: ReconcilePageProps) {
  const scope = useScope();
  const type = search.type ?? DEFAULT_TYPE;
  const status = search.status ?? 'open';
  const bulkFilter = useMemo(
    () => ({
      branch: scope.branch,
      type,
      ...(search.kind === undefined ? {} : { kind: search.kind }),
      ...(search.attribute === undefined ? {} : { attribute: search.attribute }),
      ...(search.source === undefined ? {} : { source: search.source }),
    }),
    [scope.branch, type, search.kind, search.attribute, search.source],
  );
  const filter = useMemo<CandidateFilter>(
    () => (status === 'all' ? bulkFilter : { ...bulkFilter, status }),
    [bulkFilter, status],
  );
  const candidates = useCandidates(scope.domain, filter);
  const loaded = useMemo(
    () => rankCandidates(candidates.data?.pages.flatMap((page) => page.items) ?? []),
    [candidates.data],
  );
  const shown = useMemo(
    () =>
      search.signal === undefined ? loaded : loaded.filter((c) => search.signal! in c.signals),
    [loaded, search.signal],
  );

  // The selection belongs to one set of filters; changing them starts a new one.
  const filterKey = JSON.stringify([filter, search.signal]);
  const [selection, setSelection] = useState<{ key: string; ids: ReadonlySet<string> }>({
    key: filterKey,
    ids: new Set(),
  });
  const selected = selection.key === filterKey ? selection.ids : new Set<string>();
  const select = (ids: ReadonlySet<string>) => {
    setSelection({ key: filterKey, ids });
  };

  return (
    <>
      <Filters
        key={search.source ?? ''}
        search={search}
        status={status}
        signals={signalNames(loaded)}
        onSearch={onSearch}
      />
      <BulkBar
        selected={[...selected]}
        filter={bulkFilter}
        onDone={() => {
          select(new Set());
        }}
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(16rem,24rem)_minmax(0,1fr)]">
        <CandidateList
          query={candidates}
          candidates={shown}
          active={search.candidate ?? null}
          selected={selected}
          onSelect={select}
          onOpen={(id) => {
            onSearch({ candidate: id }, true);
          }}
        />
        {search.candidate === undefined ? (
          <EmptyState>{text.list.pick}</EmptyState>
        ) : (
          <CandidateDetail key={search.candidate} id={search.candidate} />
        )}
      </div>
    </>
  );
}

/**
 * Reconciliation: the candidates of the Branch on screen, one tab per type with its count,
 * filters, a bulk decision over a selection or the filters, and the candidate on screen.
 */
export function ReconcilePage({ search, onSearch }: ReconcilePageProps) {
  const scope = useScope();
  const api = useKeeperApi();
  const toast = useToast();
  const type = search.type ?? DEFAULT_TYPE;
  const status = search.status ?? 'open';
  const countFilters = CANDIDATE_TYPES.map((t): CandidateFilter => ({
    branch: scope.branch,
    type: t,
    ...(status === 'all' ? {} : { status }),
    ...(search.kind === undefined ? {} : { kind: search.kind }),
    ...(search.attribute === undefined ? {} : { attribute: search.attribute }),
    ...(search.source === undefined ? {} : { source: search.source }),
  }));
  const counts = useCandidateCounts(scope.domain, countFilters);
  const sweep = useMutation({
    mutationFn: () => api.reconciliation.sweep(scope.domain, scope.branch),
    onSuccess: (queued) => {
      toast.notify(text.sweep.queued(queued.job_id, queued.branch), 'success');
    },
  });

  return (
    <>
      <PageHeader
        title={text.title}
        description={text.description}
        docs={text.docs}
        actions={
          <Button
            disabled={sweep.isPending}
            onClick={() => {
              sweep.mutate();
            }}
          >
            {text.sweep.run}
          </Button>
        }
      />
      <Tabs
        label={text.tabsLabel}
        value={type}
        onChange={(id) => {
          const next = CANDIDATE_TYPES.find((t) => t === id) ?? DEFAULT_TYPE;
          onSearch({
            type: next === DEFAULT_TYPE ? undefined : next,
            candidate: undefined,
            signal: undefined,
          });
        }}
        tabs={CANDIDATE_TYPES.map((t: CandidateType, index) => {
          const count = counts[index]?.data;
          return {
            id: t,
            label: text.tab(
              text.types[t],
              count === undefined ? text.countPending : countLabel(count.count, count.more),
            ),
            content: t === type ? <Queue search={search} onSearch={onSearch} /> : null,
          };
        })}
      />
    </>
  );
}
