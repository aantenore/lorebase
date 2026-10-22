import type { CanvasConfig } from '@/config/env';

/** The Sigma settings that thin out what is drawn; anything not named keeps Sigma's default. */
export interface RenderDetail {
  renderEdgeLabels?: boolean;
  hideEdgesOnMove?: boolean;
  labelRenderedSizeThreshold?: number;
}

/**
 * Less is drawn as the graph grows: edge labels disappear, edges are hidden while the view
 * moves, and only larger nodes (hubs) keep a label. Limits come from `CanvasConfig.detail`.
 */
export function renderDetail(
  nodeCount: number,
  edgeCount: number,
  detail: CanvasConfig['detail'],
): RenderDetail {
  return {
    ...(edgeCount > detail.edgeLabelsMaxEdges ? { renderEdgeLabels: false } : {}),
    ...(edgeCount >= detail.hideEdgesOnMoveMinEdges ? { hideEdgesOnMove: true } : {}),
    ...(nodeCount > detail.labelsMaxNodes
      ? { labelRenderedSizeThreshold: detail.denseLabelMinSize }
      : {}),
  };
}
