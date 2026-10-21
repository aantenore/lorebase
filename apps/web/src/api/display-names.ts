/**
 * Display names a schema document leaves out. Keeper stores an empty display name when the
 * document gives none, and the screens show these defaults instead.
 */
export const humanize = (name: string): string =>
  name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

export const DEFAULT_DISPLAY = {
  kind: (name: string) => name,
  attribute: humanize,
  relation: (name: string) => name.replace(/_/g, ' '),
  trait: (name: string) => name,
  operation: (name: string) => name,
  derivation: humanize,
  source: (name: string) => name,
} as const;
