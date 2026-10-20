import { Link, useNavigate } from '@tanstack/react-router';
import { useMemo, useState } from 'react';
import { useFirstRecordRefs, useNeighborhood, useSchema } from '@/api/queries';
import type { Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { Badge, EmptyState, PageHeader, PlaceholderSection, QueryBoundary } from '@/ui/layout';
import { Tabs } from '@/ui/tabs';
import { GraphCanvas } from './graph-canvas';
import { isCanvasView, type CanvasView } from './search';
import { fromNeighborhood, fromSchema, type GraphData } from './graph-model';

function GraphLists({ data }: { data: GraphData }) {
  return (
    <details className="mt-3 rounded-md border border-line bg-surface p-3">
      <summary className="cursor-pointer text-sm font-medium">
        List view: {data.nodes.length} nodes, {data.edges.length} edges
      </summary>
      <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        {data.edges.map((edge) => (
          <li key={edge.id}>
            {edge.source} <span className="text-muted">{edge.label}</span> {edge.target}
          </li>
        ))}
      </ul>
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
    <aside
      aria-label="Selection"
      className="mt-3 rounded-md border border-line bg-surface p-3 text-sm"
    >
      {node === undefined ? (
        <p className="text-muted">Select a node to see details.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{node.label}</span>
          <Badge>{node.group}</Badge>
          {mode === 'records' ? (
            <RecordLink domain={domain} recordRef={node.id}>
              Open Record
            </RecordLink>
          ) : (
            <Link
              to="/$domain/modeler"
              params={{ domain }}
              search={{ ...scopeSearch, tab: 'kinds' }}
              className="text-accent hover:underline"
            >
              Open in Modeler
            </Link>
          )}
        </div>
      )}
    </aside>
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

  if (ready && seeds.length === 0)
    return <EmptyState>No Records to draw on this Branch.</EmptyState>;
  if (data === null) {
    return neighborhood.isError ? (
      <p role="alert" className="text-danger">
        {neighborhood.error.message}
      </p>
    ) : (
      <p role="status" className="text-muted">
        Loading...
      </p>
    );
  }
  return (
    <>
      <GraphCanvas data={data} label="Records graph" onSelectNode={setSelected} />
      {neighborhood.data?.truncated === true && (
        <p className="mt-2 text-xs text-warning">
          Showing the first {config.canvas.maxNodes} nodes; the neighborhood is larger.
        </p>
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
      <GraphCanvas data={data} label="Schema graph" onSelectNode={setSelected} />
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
      <PageHeader
        title="Canvas"
        description={`Graph of ${scope.domainInfo.display_name} on ${scope.branch}.`}
      />
      <QueryBoundary query={schema}>
        {(data) => (
          <Tabs
            label="Canvas views"
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
                label: 'Graph explorer',
                content: <GraphExplorer schema={data} seed={seed} />,
              },
              { id: 'schema', label: 'Schema graph', content: <SchemaGraph schema={data} /> },
              {
                id: 'diff',
                label: 'Diff overlay',
                content: (
                  <PlaceholderSection
                    area="Canvas"
                    screen="Diff overlay"
                    content="Compare two refs: added, removed and modified nodes and edges colored."
                  />
                ),
              },
            ]}
          />
        )}
      </QueryBoundary>
    </>
  );
}
