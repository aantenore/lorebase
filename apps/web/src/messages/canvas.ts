/** Strings of the Canvas screen. */
export const canvas = {
  title: 'Canvas',
  description: 'Explore Records and the schema as a graph.',
  docs: 'tour/#canvas',
  tabsLabel: 'Canvas views',
  tabGraph: 'Records graph',
  tabSchema: 'Schema graph',
  graphLabel: 'Records graph',
  schemaLabel: 'Schema graph',
  noRecords: 'No Records to draw.',
  truncated: (n: number) => `Showing the first ${String(n)} nodes; the neighborhood is larger.`,
  list: {
    summary: (nodes: number, edges: number) =>
      `List view: ${String(nodes)} nodes, ${String(edges)} edges`,
    truncated: (shown: number, total: number) =>
      `Showing the first ${String(shown)} of ${String(total)} edges.`,
  },
  selection: {
    label: 'Selection',
    empty: 'Select a node to see details.',
    openRecord: 'Open Record',
    openInModeler: 'Open in Modeler',
  },
  renderer: {
    graphSummary: (label: string, nodes: number, edges: number) =>
      `${label}: ${String(nodes)} nodes, ${String(edges)} edges`,
    loadingGraph: 'Loading graph...',
    unavailable: 'Graph drawing is unavailable in this environment. Use the list below.',
    pause: 'Pause',
    resume: 'Resume',
    layout: {
      idle: 'Layout ready',
      running: 'Laying out...',
      settled: 'Layout settled',
      timeout: 'Layout paused at its time limit',
      stopped: 'Layout paused',
    },
  },
} as const;
