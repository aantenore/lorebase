import { useMemo } from 'react';
import { useNeighborhood } from '@/api/queries';
import type { Kind } from '@/api/types';
import { useScope } from '@/app/scope';
import { useConfig } from '@/config/context';
import { fromNeighborhood } from '@/features/canvas/graph-model';
import { LazyGraphCanvas } from '@/features/canvas/lazy-graph-canvas';
import { EmptyState } from '@/ui/layout';
import { labHighlight } from './lab-graph';

/** The Records the lab involved, drawn with their immediate neighbors; the involved ones stand out. */
export function LabCanvas({ involved, kinds }: { involved: string[]; kinds: Kind[] }) {
  const scope = useScope();
  const { canvas } = useConfig();
  const seeds = involved.slice(0, canvas.maxNodes);
  const neighborhood = useNeighborhood(
    scope,
    { seeds, depth: 1, max_nodes: canvas.maxNodes },
    seeds.length > 0,
  );
  const data = useMemo(
    () => (neighborhood.data === undefined ? null : fromNeighborhood(neighborhood.data, kinds)),
    [neighborhood.data, kinds],
  );
  const highlight = useMemo(
    () => (data === null ? undefined : labHighlight(data, involved)),
    [data, involved],
  );

  if (seeds.length === 0) return <EmptyState>No Records involved yet.</EmptyState>;
  if (neighborhood.isError) {
    return (
      <p role="alert" className="text-danger">
        {neighborhood.error.message}
      </p>
    );
  }
  if (data === null) {
    return (
      <p role="status" className="text-muted">
        Loading...
      </p>
    );
  }
  return (
    <>
      <LazyGraphCanvas data={data} label="Involved Records" highlight={highlight} />
      <p className="mt-2 text-xs text-muted">
        {involved.length} Records involved (search hits of both sets and the pack), drawn with their
        neighbors.
      </p>
    </>
  );
}
