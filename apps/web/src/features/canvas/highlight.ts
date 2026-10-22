/**
 * Emphasis for part of a graph: the nodes and edges named here stay as they are, the rest is
 * dimmed. Kept apart from the renderer so the rule is plain data in, plain styles out.
 */
export interface GraphHighlight {
  /** Node ids to emphasize. */
  nodes: ReadonlySet<string> | readonly string[];
  /** Edge ids to emphasize. */
  edges?: ReadonlySet<string> | readonly string[] | undefined;
}

export interface HighlightSets {
  nodes: ReadonlySet<string>;
  edges: ReadonlySet<string>;
}

/** Opacity of what is not highlighted. */
export const DIMMED_NODE_OPACITY = 0.2;
export const DIMMED_EDGE_OPACITY = 0.12;
/** Edge width factor of a highlighted edge. */
export const HIGHLIGHTED_EDGE_SCALE = 2.5;

const asSet = (ids: ReadonlySet<string> | readonly string[] | undefined): ReadonlySet<string> =>
  ids === undefined ? new Set<string>() : ids instanceof Set ? ids : new Set(ids);

export function toSets(highlight: GraphHighlight): HighlightSets {
  return { nodes: asSet(highlight.nodes), edges: asSet(highlight.edges) };
}

export interface NodeEmphasis {
  opacity?: number;
  zIndex?: number;
  labelVisibility?: 'visible' | 'hidden';
}

export interface EdgeEmphasis {
  opacity?: number;
  zIndex?: number;
  size?: number;
}

/** Display overrides of a node; none (an empty object) when nothing is highlighted. */
export function nodeEmphasis(sets: HighlightSets | null, id: string): NodeEmphasis {
  if (sets === null) return {};
  return sets.nodes.has(id)
    ? { zIndex: 1, labelVisibility: 'visible' }
    : { opacity: DIMMED_NODE_OPACITY, labelVisibility: 'hidden' };
}

export function edgeEmphasis(sets: HighlightSets | null, id: string, size: number): EdgeEmphasis {
  if (sets === null) return {};
  return sets.edges.has(id)
    ? { zIndex: 1, size: size * HIGHLIGHTED_EDGE_SCALE }
    : { opacity: DIMMED_EDGE_OPACITY };
}
