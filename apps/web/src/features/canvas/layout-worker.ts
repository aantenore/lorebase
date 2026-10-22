import type Graph from 'graphology';
import type { ForceAtlas2Settings } from 'graphology-layout-forceatlas2';
import type { LayoutRunner } from './layout-controller';

/** Whether this environment can run the layout off the main thread. */
export function workerLayoutSupported(): boolean {
  return typeof Worker !== 'undefined' && typeof Blob !== 'undefined';
}

/**
 * ForceAtlas2 in a web worker, through the supervisor of graphology-layout-forceatlas2. The
 * supervisor ships its worker as a blob built from its own source and writes the positions into
 * the graph after every iteration. It is imported on demand so the code loads with the Canvas
 * and not with the first screen. The worker is created idle: `start` begins iterating.
 */
export async function createForceAtlasRunner(
  graph: Graph,
  settings: ForceAtlas2Settings,
): Promise<LayoutRunner> {
  const { default: Supervisor } = await import('graphology-layout-forceatlas2/worker');
  return new Supervisor(graph, { settings });
}
