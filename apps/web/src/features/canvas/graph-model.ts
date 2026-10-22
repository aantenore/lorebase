import Graph from 'graphology';
import forceAtlas2, { type ForceAtlas2Settings } from 'graphology-layout-forceatlas2';
import type { Kind, NeighborhoodResponse, Schema } from '@/api/types';

export interface CanvasNode {
  id: string;
  label: string;
  /** Grouping key, used for color: the Kind api name. */
  group: string;
  color: string;
}

export interface CanvasEdge {
  id: string;
  source: string;
  target: string;
  label: string;
}

export interface GraphData {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
}

/** Colors for Kinds that carry no presentation hint of their own. */
const FALLBACK_COLORS = ['#3a4fd6', '#1a7f4b', '#b25e09', '#8a3ffc', '#0b7285', '#a61e4d'];

/** Node radius in pixels: grows with degree so hubs stand out (design 12.3). */
const MIN_NODE_SIZE = 8;
const NODE_SIZE_PER_EDGE = 2;
const MAX_NODE_SIZE = 22;

function colorOf(kinds: Kind[], name: string): string {
  const index = kinds.findIndex((k) => k.api_name === name);
  const kind = kinds[index];
  return kind?.color ?? FALLBACK_COLORS[Math.max(index, 0) % FALLBACK_COLORS.length] ?? '#888888';
}

export function fromNeighborhood(response: NeighborhoodResponse, kinds: Kind[]): GraphData {
  return {
    nodes: response.nodes.map((n) => ({
      id: n.ref,
      label: n.title,
      group: n.kind,
      color: colorOf(kinds, n.kind),
    })),
    edges: response.edges.map((e, index) => ({
      id: `e${index}`,
      source: e.from,
      target: e.to,
      label: e.relation,
    })),
  };
}

/** Schema graph: Kinds as nodes, Relations as edges labelled with cardinality. */
export function fromSchema(schema: Schema): GraphData {
  const byId = new Map(schema.kinds.map((k) => [k.id, k]));
  return {
    nodes: schema.kinds.map((k) => ({
      id: k.api_name,
      label: k.display_name,
      group: k.api_name,
      color: colorOf(schema.kinds, k.api_name),
    })),
    edges: schema.relations.flatMap((r) => {
      const from = byId.get(r.from_kind);
      const to = byId.get(r.to_kind);
      return from === undefined || to === undefined
        ? []
        : [
            {
              id: r.id,
              source: from.api_name,
              target: to.api_name,
              label: `${r.api_name} (${r.cardinality})`,
            },
          ];
    }),
  };
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/**
 * ForceAtlas2 settings for a graph: tuned by size, with the Barnes-Hut approximation from
 * `barnesHutMinNodes` nodes on (it trades a little accuracy for far fewer node pairs).
 */
export function layoutSettings(graph: Graph, barnesHutMinNodes: number): ForceAtlas2Settings {
  return {
    ...forceAtlas2.inferSettings(graph),
    barnesHutOptimize: graph.order >= barnesHutMinNodes,
  };
}

/**
 * Build a positioned graphology graph: nodes seeded evenly over a disc, then refined by
 * `layoutIterations` ForceAtlas2 iterations on the calling thread (0 leaves the seed as it is,
 * for a layout that runs elsewhere).
 */
export function buildGraph(data: GraphData, layoutIterations: number): Graph {
  const graph = new Graph({ type: 'directed', multi: true });
  const count = Math.max(data.nodes.length, 1);
  data.nodes.forEach((node, index) => {
    // Sunflower seed: even density and no symmetry for the layout to get stuck in.
    const angle = index * GOLDEN_ANGLE;
    const radius = Math.sqrt((index + 0.5) / count);
    graph.addNode(node.id, {
      label: node.label,
      color: node.color,
      group: node.group,
      size: MIN_NODE_SIZE,
      x: radius * Math.cos(angle),
      y: radius * Math.sin(angle),
    });
  });
  for (const edge of data.edges) {
    if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
      graph.addEdgeWithKey(edge.id, edge.source, edge.target, { label: edge.label, size: 1 });
    }
  }
  graph.forEachNode((node) => {
    const size = Math.min(MAX_NODE_SIZE, MIN_NODE_SIZE + graph.degree(node) * NODE_SIZE_PER_EDGE);
    graph.setNodeAttribute(node, 'size', size);
  });
  if (graph.order > 1 && layoutIterations > 0) {
    forceAtlas2.assign(graph, {
      iterations: layoutIterations,
      settings: forceAtlas2.inferSettings(graph),
    });
  }
  return graph;
}
