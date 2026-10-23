import { useMemo } from 'react';
import type { Kind, TraceStep } from '@/api/types';
import { LazyGraphCanvas } from '@/features/canvas/lazy-graph-canvas';
import { agentActivity as agentActivityMessages } from '@/messages/agent-activity';
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
        <span className="font-medium">{agentActivityMessages.canvas.step}</span>
        <input
          type="range"
          min={first}
          max={last}
          step={1}
          value={selected}
          aria-label={agentActivityMessages.canvas.step}
          aria-valuetext={agentActivityMessages.canvas.valueText(selected, last)}
          onChange={(event) => {
            onSelect(Number(event.target.value));
          }}
          className="flex-1"
        />
        <span className="w-20 text-right text-muted">
          {agentActivityMessages.canvas.position(selected, last)}
        </span>
      </label>
      {data.nodes.length === 0 ? (
        <EmptyState>{agentActivityMessages.canvas.empty}</EmptyState>
      ) : (
        <LazyGraphCanvas
          data={data}
          label={agentActivityMessages.canvas.label}
          highlight={highlight}
        />
      )}
      <p className="text-xs text-muted">
        {agentActivityMessages.canvas.summary(
          data.nodes.length,
          data.edges.length,
          first,
          selected,
        )}
        {highlight === undefined
          ? '.'
          : agentActivityMessages.canvas.byStep([...highlight.nodes].length, selected)}
      </p>
    </div>
  );
}
