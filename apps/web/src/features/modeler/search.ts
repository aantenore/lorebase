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

export const MODELER_VIEWS = ['overview', 'edit'] as const;

export type ModelerView = (typeof MODELER_VIEWS)[number];

export function isModelerView(value: unknown): value is ModelerView {
  return MODELER_VIEWS.some((view) => view === value);
}
