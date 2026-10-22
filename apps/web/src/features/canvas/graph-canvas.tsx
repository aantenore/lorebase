import type Sigma from 'sigma';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useConfig } from '@/config/context';
import { Button } from '@/ui/button';
import { useTheme } from '@/ui/theme';
import { buildGraph, layoutSettings, type GraphData } from './graph-model';
import {
  edgeEmphasis,
  nodeEmphasis,
  toSets,
  type GraphHighlight,
  type HighlightSets,
} from './highlight';
import { LayoutController, type LayoutStatus } from './layout-controller';
import { createForceAtlasRunner, workerLayoutSupported } from './layout-worker';
import { renderDetail } from './render-detail';

/** Free space around the graph, in pixels, so labels at the edge stay visible. */
const STAGE_PADDING = 60;

const LAYOUT_LABELS: Record<LayoutStatus, string> = {
  idle: 'Layout ready',
  running: 'Laying out...',
  settled: 'Layout settled',
  timeout: 'Layout paused at its time limit',
  stopped: 'Layout paused',
};

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value === '' ? fallback : value;
}

export interface GraphCanvasProps {
  data: GraphData;
  label: string;
  onSelectNode?: (id: string | null) => void;
  /**
   * Nodes and edges (by id) to emphasize; everything else is dimmed. Without it the graph is
   * drawn as it is. Pass a memoized value: a new object redraws the emphasis.
   */
  highlight?: GraphHighlight | undefined;
}

/**
 * Sigma.js renderer over a graphology graph. WebGL can be unavailable (tests, remote
 * desktops); then a message replaces the canvas and the textual list still describes
 * the graph.
 */
export function GraphCanvas({ data, label, onSelectNode, highlight }: GraphCanvasProps) {
  const config = useConfig();
  const { theme } = useTheme();
  const container = useRef<HTMLDivElement>(null);
  // Sigma 4 renders with WebGL 2; without it the canvas is replaced by a message.
  const [failed, setFailed] = useState(() => typeof WebGL2RenderingContext === 'undefined');

  const { canvas } = config;
  // The layout controller of the current graph; null where there is no layout to control.
  const controller = useRef<LayoutController | null>(null);
  // Null until a worker layout exists for the graph (no worker: positions stay as built).
  const [layout, setLayout] = useState<LayoutStatus | null>(null);
  // The click handler is read through a ref so a new callback never rebuilds the renderer.
  const selectNode = useRef(onSelectNode);
  useEffect(() => {
    selectNode.current = onSelectNode;
  }, [onSelectNode]);

  // The emphasis is read by the renderer's reducers through a ref, so a new highlight redraws
  // the graph without rebuilding the renderer or restarting the layout.
  const emphasis = useRef<HighlightSets | null>(null);
  const sigma = useRef<Sigma | null>(null);
  useEffect(() => {
    emphasis.current = highlight === undefined ? null : toSets(highlight);
    sigma.current?.refresh();
  }, [highlight]);

  // Small graphs are laid out here, before the first draw; larger ones are laid out in a worker
  // after it, so the first draw does not wait for the layout.
  const graph = useMemo(
    () =>
      buildGraph(data, data.nodes.length <= canvas.prelayoutMaxNodes ? canvas.layoutIterations : 0),
    [data, canvas.prelayoutMaxNodes, canvas.layoutIterations],
  );

  // The layout belongs to the graph: it starts once per graph and is not touched by renders,
  // theme changes or a new selection handler.
  useEffect(() => {
    if (failed || graph.order < 2 || !workerLayoutSupported()) return;
    let cancelled = false;
    let created: LayoutController | null = null;
    let unsubscribe: (() => void) | null = null;
    void createForceAtlasRunner(graph, layoutSettings(graph, canvas.layout.barnesHutMinNodes))
      .then((runner) => {
        if (cancelled) {
          runner.kill();
          return;
        }
        created = new LayoutController(graph, runner, canvas.layout);
        controller.current = created;
        unsubscribe = created.subscribe(setLayout);
        created.start();
        setLayout(created.status);
      })
      .catch((error: unknown) => {
        console.error('The graph layout could not start.', error);
      });
    return () => {
      cancelled = true;
      unsubscribe?.();
      created?.dispose();
      controller.current = null;
    };
  }, [graph, failed, canvas.layout]);

  useEffect(() => {
    const element = container.current;
    if (element === null || failed) return;
    let cancelled = false;
    let renderer: Sigma | null = null;
    // Loaded on demand: the renderer needs WebGL 2 at import time and is a large chunk.
    void import('sigma')
      .then(({ default: SigmaRenderer }) => {
        if (cancelled) return;
        renderer = new SigmaRenderer(graph, element, {
          nodeReducer: (key) => nodeEmphasis(emphasis.current, key),
          edgeReducer: (key, display) => edgeEmphasis(emphasis.current, key, display.size),
          settings: {
            itemSizesReference: 'screen',
            autoRescaleContent: 'positions',
            stagePadding: STAGE_PADDING,
            ...renderDetail(graph.order, graph.size, canvas.detail),
          },
          styles: {
            nodes: {
              color: { attribute: 'color' },
              size: { attribute: 'size' },
              label: { attribute: 'label' },
              labelColor: cssVar('--lb-text', '#161a22'),
            },
            edges: {
              color: cssVar('--lb-line', '#9aa4b4'),
              label: { attribute: 'label' },
              labelColor: cssVar('--lb-muted', '#5a6372'),
            },
          },
        });
        sigma.current = renderer;
        renderer.on('clickNode', ({ node }) => {
          selectNode.current?.(node);
        });
        renderer.on('clickStage', () => {
          selectNode.current?.(null);
        });
      })
      .catch((error: unknown) => {
        console.error('The graph renderer could not start.', error);
        // The renderer could not start (for example no usable GPU).
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      sigma.current = null;
      renderer?.kill();
    };
    // The theme choice is a dependency so colors are re-read when it changes.
  }, [graph, theme, failed, canvas.detail]);

  return (
    <div
      role="img"
      aria-label={`${label}: ${data.nodes.length} nodes, ${data.edges.length} edges`}
      data-layout={layout ?? undefined}
      className="relative h-[28rem] rounded-md border border-line bg-surface"
    >
      <div ref={container} className="size-full" />
      {layout !== null && !failed && (
        <div className="absolute top-2 left-2 flex items-center gap-2 rounded-md border border-line bg-surface/90 px-2 py-1 text-xs text-muted">
          <span role="status">{LAYOUT_LABELS[layout]}</span>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (layout === 'running') controller.current?.stop();
              else controller.current?.start();
            }}
          >
            {layout === 'running' ? 'Pause' : 'Resume'}
          </Button>
        </div>
      )}
      {failed && (
        <p
          role="status"
          className="absolute inset-0 grid place-items-center p-4 text-center text-muted"
        >
          Graph drawing is unavailable in this environment. Use the list below.
        </p>
      )}
    </div>
  );
}
