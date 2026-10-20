/** Screen inventory from docs/design.md section 12.3. */
export type Area = 'Explorer' | 'Canvas' | 'Modeler' | 'Review' | 'Sources' | 'History' | 'Admin';

export interface ScreenSpec {
  area: Area;
  screen: string;
  content: string;
}

export const SCREEN_INVENTORY: readonly ScreenSpec[] = [
  {
    area: 'Explorer',
    screen: 'Domain home',
    content: 'Kinds with counts and descriptions, recent Revisions, open Proposals, Source health.',
  },
  {
    area: 'Explorer',
    screen: 'Search results',
    content: 'Mixed-Kind results, facets by Kind and indexed Attributes, snippets.',
  },
  {
    area: 'Explorer',
    screen: 'Kind table',
    content:
      'Columns picker, filters builder (DSL), sort, saved views, CSV export (permission-gated).',
  },
  {
    area: 'Explorer',
    screen: 'Record page',
    content:
      'Attributes with type-aware rendering, derived values marked, linked Records grouped by Relation with counts, Lineage popover per value, history timeline, Run Operation panel with forms built from parameters, open in Canvas.',
  },
  { area: 'Explorer', screen: 'Trait view', content: 'Records across implementing Kinds.' },
  {
    area: 'Canvas',
    screen: 'Graph explorer',
    content:
      'Start from Records or search; expand neighbors by Relation; filter by Kind and Relation; ForceAtlas2 layout in a web worker; node size by degree, color by Kind; selection panel; path between two Records; caps with progressive loading.',
  },
  {
    area: 'Canvas',
    screen: 'Schema graph',
    content:
      'Kinds as nodes, Relations as edges with cardinality, Traits as groups; click through to Modeler.',
  },
  {
    area: 'Canvas',
    screen: 'Diff overlay',
    content:
      'Compare two refs: added, removed and modified nodes and edges colored; filter to changes only.',
  },
  {
    area: 'Modeler',
    screen: 'Schema overview',
    content: 'Kinds, Relations, Traits, Operations, Derivations, Sources lists; validation panel.',
  },
  {
    area: 'Modeler',
    screen: 'Kind editor',
    content:
      'Attributes table (type, required, unique, constraints, sensitivity, indexed, searchable), primary key and title, Traits, search and embedding template, built-in Operations toggles.',
  },
  {
    area: 'Modeler',
    screen: 'Relation editor',
    content: 'Endpoints, cardinality, required, link Attributes, delete behavior.',
  },
  {
    area: 'Modeler',
    screen: 'Trait editor',
    content: 'Attributes and implementing Kinds with mappings.',
  },
  {
    area: 'Modeler',
    screen: 'Operation editor',
    content:
      'Parameters, preconditions, steps, review policy, permissions; YAML and form views kept in sync; test bench with dry run against the current Branch.',
  },
  {
    area: 'Modeler',
    screen: 'Derivation editor',
    content: 'Expression or aggregate definition, preview on sample Records.',
  },
  {
    area: 'Modeler',
    screen: 'Source mapping editor',
    content:
      'Connection reference, inferred columns, column-to-Attribute mapping with transforms, live preview of sample rows mapped to Records, sync settings.',
  },
  {
    area: 'Modeler',
    screen: 'Change set summary',
    content:
      'Pending schema changes on the Branch, data migrations they imply (casts, defaults), Open Proposal.',
  },
  {
    area: 'Review',
    screen: 'Proposal list',
    content:
      'Filters: open, assigned to me, origin (manual, Source, extraction, Operation), status of checks.',
  },
  {
    area: 'Review',
    screen: 'Proposal detail',
    content:
      'Summary, checks, reviewers, schema diff, data diff by Kind with counts, Record-level side-by-side diff, comments, merge button.',
  },
  {
    area: 'Review',
    screen: 'Extraction items',
    content:
      'Item list with confidence, match status and evidence; document viewer with highlighted spans; accept or reject per item, bulk by threshold or filter; ambiguous match picker.',
  },
  {
    area: 'Review',
    screen: 'Conflict resolver',
    content:
      'Conflict list by code; base, ours, theirs panes; choices and custom value editor; grouped Conflicts with bulk actions; policy application; progress; re-validation status.',
  },
  {
    area: 'Sources',
    screen: 'Source list and detail',
    content: 'Status, schedule, last runs, stats, errors, mapping link, run now.',
  },
  {
    area: 'Sources',
    screen: 'Documents and extraction jobs',
    content:
      'Library, upload, jobs with progress, token usage and cost estimate, link to Proposal.',
  },
  {
    area: 'History',
    screen: 'Log and compare',
    content: 'Branch log, Revision detail with diff, compare any two refs, tags.',
  },
  { area: 'Admin', screen: 'Members and roles', content: 'Users, groups, grants by scope.' },
  {
    area: 'Admin',
    screen: 'API keys and service principals',
    content: 'Create, scope, expire, revoke; last used.',
  },
  {
    area: 'Admin',
    screen: 'Audit log',
    content: 'Filterable, exportable, chain verification status.',
  },
  {
    area: 'Admin',
    screen: 'Domain settings',
    content:
      'Default Branch, protection rules, materialization, search language, extraction defaults.',
  },
];

export function screensOf(area: Area): ScreenSpec[] {
  return SCREEN_INVENTORY.filter((s) => s.area === area);
}
