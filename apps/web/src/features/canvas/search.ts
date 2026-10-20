export type CanvasView = 'graph' | 'schema' | 'diff';

/** Kept apart from the page component so route validation does not pull in the renderer. */
export function isCanvasView(value: unknown): value is CanvasView {
  return value === 'graph' || value === 'schema' || value === 'diff';
}
