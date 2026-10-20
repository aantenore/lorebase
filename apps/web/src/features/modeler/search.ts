export const MODELER_TABS = [
  'kinds',
  'attributes',
  'relations',
  'traits',
  'operations',
  'derivations',
  'sources',
] as const;

export type ModelerTab = (typeof MODELER_TABS)[number];

export function isModelerTab(value: unknown): value is ModelerTab {
  return MODELER_TABS.some((tab) => tab === value);
}
