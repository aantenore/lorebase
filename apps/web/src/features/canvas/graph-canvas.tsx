import type Sigma from 'sigma';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useConfig } from '@/config/context';
import { useTheme } from '@/ui/theme';
import { buildGraph, type GraphData } from './graph-model';

/** Free space around the graph, in pixels, so labels at the edge stay visible. */
const STAGE_PADDING = 60;

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value === '' ? fallback : value;
}

export interface GraphCanvasProps {
  data: GraphData;
  label: string;
  onSelectNode?: (id: string | null) => void;
}

/**
 * Sigma.js renderer over a graphology graph. WebGL can be unavailable (tests, remote
 * desktops); then a message replaces the canvas and the textual list still describes
 * the graph.
 */
export function GraphCanvas({ data, label, onSelectNode }: GraphCanvasProps) {
  const config = useConfig();
  const { theme } = useTheme();
  const container = useRef<HTMLDivElement>(null);
  // Sigma 4 renders with WebGL 2; without it the canvas is replaced by a message.
  const [failed, setFailed] = useState(() => typeof WebGL2RenderingContext === 'undefined');

  const graph = useMemo(
    () => buildGraph(data, config.canvas.layoutIterations),
    [data, config.canvas.layoutIterations],
  );

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
          settings: {
            itemSizesReference: 'screen',
            autoRescaleContent: 'positions',
            stagePadding: STAGE_PADDING,
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
        renderer.on('clickNode', ({ node }) => {
          onSelectNode?.(node);
        });
        renderer.on('clickStage', () => {
          onSelectNode?.(null);
        });
      })
      .catch((error: unknown) => {
        console.error('The graph renderer could not start.', error);
        // The renderer could not start (for example no usable GPU).
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      renderer?.kill();
    };
    // The theme choice is a dependency so colors are re-read when it changes.
  }, [graph, onSelectNode, theme, failed]);

  return (
    <div
      role="img"
      aria-label={`${label}: ${data.nodes.length} nodes, ${data.edges.length} edges`}
      className="relative h-[28rem] rounded-md border border-line bg-surface"
    >
      <div ref={container} className="size-full" />
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
