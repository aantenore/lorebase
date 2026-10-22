import { useMemo } from 'react';
import type { Kind, TraceStep } from '@/api/types';
import { LazyGraphCanvas } from '@/features/canvas/lazy-graph-canvas';
import { EmptyState } from '@/ui/layout';
import { traceGraph } from './trace-graph';

/**
 * The Records and Links a session touched, up to a step the slider picks; what the step itself
 * touched is emphasized and the rest of the graph dimmed.
 */
export function TraceCanvas({
  steps,
  kinds,
  selected,
  onSelect,
}: {
  steps: TraceStep[];
  kinds: Kind[];
  selected: number;
  onSelect: (step: number) => void;
}) {
  const { data, highlight } = useMemo(
    () => traceGraph(steps, selected, kinds),
    [steps, selected, kinds],
  );
  const first = steps[0]?.step ?? 1;
  const last = steps[steps.length - 1]?.step ?? first;

  return (
    <div className="flex flex-col gap-2">
      <label className="flex items-center gap-3 text-sm">
        <span className="font-medium">Step</span>
        <input
          type="range"
          min={first}
          max={last}
          step={1}
          value={selected}
          aria-label="Step"
          aria-valuetext={`Step ${selected.toString()} of ${last.toString()}`}
          onChange={(event) => {
            onSelect(Number(event.target.value));
          }}
          className="flex-1"
        />
        <span className="w-20 text-right text-muted">
          {selected} of {last}
        </span>
      </label>
      {data.nodes.length === 0 ? (
        <EmptyState>No Records were touched up to this step.</EmptyState>
      ) : (
        <LazyGraphCanvas data={data} label="Records touched" highlight={highlight} />
      )}
      <p className="text-xs text-muted">
        {data.nodes.length} Records and {data.edges.length} Links touched in steps {first} to{' '}
        {selected}
        {highlight === undefined
          ? '.'
          : `; ${[...highlight.nodes].length.toString()} by step ${selected.toString()}.`}
      </p>
    </div>
  );
}
