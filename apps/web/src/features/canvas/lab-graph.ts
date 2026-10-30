import type { ContextPack, SearchResult } from '@/api/types';
import type { GraphData } from './graph-model';
import type { GraphHighlight } from './highlight';

/** Every Record the pack carries, seeds and neighbors. */
export function packRefs(pack: ContextPack): string[] {
  return [...pack.seeds.map((s) => s.ref), ...pack.neighbors.map((n) => n.ref)];
}

/** The Records the lab involved: hits of both parameter sets and the Records of the pack. */
export function involvedRefs(
  a: SearchResult[] | undefined,
  b: SearchResult[] | undefined,
  pack: ContextPack | undefined,
): string[] {
  const refs = new Set<string>();
  for (const hit of a ?? []) refs.add(hit.ref);
  for (const hit of b ?? []) refs.add(hit.ref);
  for (const ref of pack === undefined ? [] : packRefs(pack)) refs.add(ref);
  return [...refs];
}

/**
 * Emphasis for the involved Records in a graph drawn around them: those nodes, and the edges
 * between two of them. Absent when none of them is on the graph, so nothing is dimmed.
 */
export function labHighlight(data: GraphData, involved: string[]): GraphHighlight | undefined {
  const set = new Set(involved);
  const nodes = new Set(data.nodes.filter((n) => set.has(n.id)).map((n) => n.id));
  if (nodes.size === 0) return undefined;
  const edges = new Set(
    data.edges.filter((e) => nodes.has(e.source) && nodes.has(e.target)).map((e) => e.id),
  );
  return { nodes, edges };
}
