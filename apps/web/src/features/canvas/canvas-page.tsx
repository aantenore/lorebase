import { Link, useNavigate } from '@tanstack/react-router';
import { useMemo, useState } from 'react';
import { useFirstRecordRefs, useNeighborhood, useSchema } from '@/api/queries';
import type { Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { canvas as canvasMessages } from '@/messages/canvas';
import { common as commonMessages } from '@/messages/common';
import { Badge, Card, EmptyState, PageHeader, QueryBoundary } from '@/ui/layout';
import { Tabs } from '@/ui/tabs';
import { GraphCanvas } from './graph-canvas';
import { isCanvasView, type CanvasView } from './search';
import { fromNeighborhood, fromSchema, type GraphData } from './graph-model';

const t = canvasMessages;

function GraphLists({ data }: { data: GraphData }) {
  const { listMaxEdges } = useConfig().canvas.detail;
  const shown = data.edges.slice(0, listMaxEdges);
  return (
    <details className="mt-4 rounded-md border border-line bg-surface p-4">
      <summary className="cursor-pointer text-sm font-medium">
        {t.list.summary(data.nodes.length, data.edges.length)}
      </summary>
      <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        {shown.map((edge) => (
          <li key={edge.id} className="min-w-0 break-all">
            {edge.source} <span className="text-muted">{edge.label}</span> {edge.target}
          </li>
        ))}
      </ul>
      {shown.length < data.edges.length && (
        <p className="mt-2 text-xs text-muted">
          {t.list.truncated(shown.length, data.edges.length)}
        </p>
      )}
    </details>
  );
}

function SelectionPanel({
  data,
  selected,
  domain,
  mode,
}: {
  data: GraphData;
  selected: string | null;
  domain: string;
  mode: 'records' | 'schema';
}) {
  const scopeSearch = useScopeSearch();
  const node = data.nodes.find((n) => n.id === selected);
  return (
    <Card as="aside" aria-label={t.selection.label} className="mt-4 text-sm">
      {node === undefined ? (
        <p className="text-muted">{t.selection.empty}</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 font-medium break-all">{node.label}</span>
          <Badge>{node.group}</Badge>
          {mode === 'records' ? (
            <RecordLink domain={domain} recordRef={node.id}>
              {t.selection.openRecord}
            </RecordLink>
          ) : (
            <Link
              to="/$domain/modeler"
              params={{ domain }}
              search={{ ...scopeSearch, tab: 'kinds' }}
              className="text-accent hover:underline"
            >
              {t.selection.openInModeler}
            </Link>
          )}
        </div>
      )}
    </Card>
  );
}

function GraphExplorer({ schema, seed }: { schema: Schema; seed: string | undefined }) {
  const scope = useScope();
  const config = useConfig();
  const [selected, setSelected] = useState<string | null>(null);

  const explicit = useMemo(
    () => (seed === undefined ? [] : seed.split(',').filter(Boolean)),
    [seed],
  );
  const defaults = useFirstRecordRefs(
    scope,
    explicit.length === 0 ? schema.kinds.map((k) => k.api_name) : [],
  );
  const seeds = explicit.length > 0 ? explicit : defaults.refs;
  const ready = explicit.length > 0 || !defaults.pending;

  const neighborhood = useNeighborhood(
    scope,
    { seeds, depth: config.canvas.depth, max_nodes: config.canvas.maxNodes },
    ready && seeds.length > 0,
  );
  const data = useMemo(
    () =>
      neighborhood.data === undefined ? null : fromNeighborhood(neighborhood.data, schema.kinds),
    [neighborhood.data, schema.kinds],
  );

  if (ready && seeds.length === 0) return <EmptyState>{t.noRecords}</EmptyState>;
  if (data === null) {
    return neighborhood.isError ? (
      <p role="alert" className="text-danger">
        {neighborhood.error.message}
      </p>
    ) : (
      <p role="status" className="text-muted">
        {commonMessages.loading}
      </p>
    );
  }
  return (
    <>
      <GraphCanvas data={data} label={t.graphLabel} onSelectNode={setSelected} />
      {neighborhood.data?.truncated === true && (
        <p className="mt-2 text-xs text-warning">{t.truncated(config.canvas.maxNodes)}</p>
      )}
      <SelectionPanel data={data} selected={selected} domain={scope.domain} mode="records" />
      <GraphLists data={data} />
    </>
  );
}

function SchemaGraph({ schema }: { schema: Schema }) {
  const scope = useScope();
  const [selected, setSelected] = useState<string | null>(null);
  const data = useMemo(() => fromSchema(schema), [schema]);
  return (
    <>
      <GraphCanvas data={data} label={t.schemaLabel} onSelectNode={setSelected} />
      <SelectionPanel data={data} selected={selected} domain={scope.domain} mode="schema" />
      <GraphLists data={data} />
    </>
  );
}

/** Canvas: graph explorer and schema graph; diff overlay is planned. */
export function CanvasPage({ view, seed }: { view: CanvasView; seed: string | undefined }) {
  const scope = useScope();
  const schema = useSchema(scope);
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();

  return (
    <>
      <PageHeader title={t.title} description={t.description} docs={t.docs} />
      <QueryBoundary query={schema}>
        {(data) => (
          <Tabs
            label={t.tabsLabel}
            value={view}
            onChange={(id) => {
              void navigate({
                to: '.',
                search: { ...scopeSearch, view: isCanvasView(id) ? id : 'graph', seed },
              });
            }}
            tabs={[
              {
                id: 'graph',
                label: t.tabGraph,
                content: <GraphExplorer schema={data} seed={seed} />,
              },
              { id: 'schema', label: t.tabSchema, content: <SchemaGraph schema={data} /> },
            ]}
          />
        )}
      </QueryBoundary>
    </>
  );
}
