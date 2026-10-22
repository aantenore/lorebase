import type { GraphEdge, GraphNode } from '../types';

/** Small deterministic generator, so a fixture is the same on every run. */
function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface PerfGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/**
 * A connected graph of `nodeCount` nodes and about `edgeCount` edges for performance runs. Each
 * node after the first attaches to an earlier one, favoring nodes that already have links, so
 * hubs appear the way they do in real data; the remaining edges join random pairs.
 */
export function generatePerfGraph(
  nodeCount: number,
  edgeCount: number,
  kinds: readonly string[],
  seed = 1,
): PerfGraph {
  const random = mulberry32(seed);
  const kindNames = kinds.length > 0 ? kinds : ['Record'];
  const nodes: GraphNode[] = Array.from({ length: nodeCount }, (_, index) => {
    const kind = kindNames[index % kindNames.length] ?? 'Record';
    return { ref: `${kind}/P-${index}`, kind, title: `${kind} ${index}` };
  });
  const edges: GraphEdge[] = [];
  // Every edge end is pushed here, so a uniform pick from it is a degree-weighted pick.
  const ends: number[] = [0];
  const link = (from: number, to: number) => {
    const a = nodes[from];
    const b = nodes[to];
    if (a === undefined || b === undefined || from === to) return;
    edges.push({ from: a.ref, to: b.ref, relation: 'related_to' });
    ends.push(from, to);
  };
  for (let index = 1; index < nodeCount; index += 1) {
    link(index, ends[Math.floor(random() * ends.length)] ?? 0);
  }
  while (edges.length < edgeCount && nodeCount > 1) {
    link(Math.floor(random() * nodeCount), Math.floor(random() * nodeCount));
  }
  return { nodes, edges };
}
