import { refOf } from '@/api/refs';
import type { Kind, TraceStep } from '@/api/types';
import type { GraphData } from '@/features/canvas/graph-model';
import type { GraphHighlight } from '@/features/canvas/highlight';
import { edgeId, refGraph, type RefLink } from '@/features/canvas/ref-graph';

/** The Records and Links one step touched, as `Kind/key` references. */
export interface StepRefs {
  records: string[];
  links: RefLink[];
}

export function stepRefs(step: TraceStep): StepRefs {
  const refs = step.refs;
  if (refs === null || refs === undefined) return { records: [], links: [] };
  return {
    records: refs.records.map((r) => refOf(r.kind, r.key)),
    links: refs.links.map((l) => ({
      relation: l.relation,
      from: refOf(l.from.kind, l.from.key),
      to: refOf(l.to.kind, l.to.key),
    })),
  };
}

export interface TraceGraph {
  /** Everything steps 1 to the selected one touched. */
  data: GraphData;
  /** What the selected step touched; absent when it touched nothing, so nothing is dimmed. */
  highlight: GraphHighlight | undefined;
}

/**
 * The graph of a session up to a step: nodes are the Records touched by steps 1 to `selected`,
 * edges their Links. The highlight is what step `selected` itself touched.
 */
export function traceGraph(steps: TraceStep[], selected: number, kinds: Kind[]): TraceGraph {
  const upTo = steps.filter((s) => s.step <= selected);
  const records = new Set<string>();
  const links: RefLink[] = [];
  for (const step of upTo) {
    const refs = stepRefs(step);
    refs.records.forEach((ref) => records.add(ref));
    links.push(...refs.links);
  }
  const data = refGraph(records, links, kinds);

  const current = upTo.find((s) => s.step === selected);
  const now = current === undefined ? { records: [], links: [] } : stepRefs(current);
  const nodes = new Set<string>(now.records);
  for (const link of now.links) {
    nodes.add(link.from);
    nodes.add(link.to);
  }
  const highlight =
    nodes.size === 0 ? undefined : { nodes, edges: new Set(now.links.map((l) => edgeId(l))) };
  return { data, highlight };
}
