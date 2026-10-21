/**
 * Extraction items in memory: listing with Keeper's filters, decisions with reviewer edits,
 * and the re-materialization of the extraction Branch, which holds exactly the accepted items
 * (docs/extraction.md, steps 6 and 7). Pure functions over `ProposalState`; the adapter wires
 * them to the `proposals.items` and `proposals.decideItems` methods.
 */
import { apiErrorFromProblem } from '../errors';
import type {
  DecideItemsInput,
  DecideItemsResult,
  ExtractionItem,
  ExtractionSummary,
  ItemDecision,
  ItemEdits,
  ItemFilter,
  ItemListParams,
  Materialized,
} from '../extraction-types';
import type { Attribute, Kind, Page, ProblemIssue, Relation } from '../types';
import { validateValue } from '../values';
import { revision as buildRevision } from './builders';
import { decodeCursor, encodeCursor } from './engine';
import {
  refOf,
  type BranchData,
  type ProposalState,
  type StoredLink,
  type StoredRecord,
} from './state';

const ACTOR = 'usr_mock';

/** The item as Keeper lists it: the reviewer's edits laid over what Scribe proposed. */
export function view(item: ExtractionItem): ExtractionItem {
  const edits = item.edits;
  if (edits === null) return structuredClone(item);
  return structuredClone({
    ...item,
    key: edits.key ?? item.key,
    value: edits.value ?? item.value,
    from: edits.from ?? item.from,
    to: edits.to ?? item.to,
    values: { ...item.values, ...edits.values },
  });
}

function matches(item: ExtractionItem, filter: ItemFilter): boolean {
  if (filter.status !== undefined && filter.status.length > 0) {
    if (!filter.status.includes(item.decision)) return false;
  }
  if (filter.type !== undefined && filter.type.length > 0 && !filter.type.includes(item.type)) {
    return false;
  }
  if (filter.kind !== undefined && filter.kind.length > 0) {
    const target = item.relation ?? item.kind;
    if (target === null || !filter.kind.includes(target)) return false;
  }
  const confidence = item.confidence ?? 0;
  if (filter.min_confidence !== undefined && confidence < filter.min_confidence) return false;
  if (filter.max_confidence !== undefined && confidence >= filter.max_confidence) return false;
  return true;
}

export function listItems(
  state: ProposalState,
  params: ItemListParams | undefined,
): Page<ExtractionItem> {
  const all = state.items
    .filter((item) => matches(view(item), params ?? {}))
    .sort((a, b) => a.seq - b.seq)
    .map((item) => withReason(state, view(item)));
  const offset = decodeCursor(params?.cursor);
  const limit = params?.limit;
  if (limit === undefined) return { items: all.slice(offset), next_cursor: null };
  return {
    items: all.slice(offset, offset + limit),
    next_cursor: offset + limit < all.length ? encodeCursor(offset + limit) : null,
  };
}

/** `not_applied` of an accepted item the last materialization left out. */
function withReason(state: ProposalState, item: ExtractionItem): ExtractionItem {
  const skipped = state.extraction?.skipped ?? {};
  return { ...item, not_applied: item.decision === 'accepted' ? (skipped[item.id] ?? null) : null };
}

export function summarize(items: ExtractionItem[]): ExtractionSummary {
  const decisions: Record<ItemDecision, number> = { pending: 0, accepted: 0, rejected: 0 };
  const types: Record<string, number> = {};
  const targets: Record<string, number> = {};
  const confidences: number[] = [];
  for (const item of items) {
    decisions[item.decision] += 1;
    types[item.type] = (types[item.type] ?? 0) + 1;
    const target = item.relation ?? item.kind ?? '';
    targets[target] = (targets[target] ?? 0) + 1;
    if (item.confidence !== null) confidences.push(item.confidence);
  }
  return {
    items: items.length,
    decisions,
    types,
    targets,
    mean_confidence:
      confidences.length === 0
        ? null
        : Math.round((confidences.reduce((a, b) => a + b, 0) / confidences.length) * 10_000) /
          10_000,
  };
}

/** The `extraction` check of a Proposal, as Keeper words it. */
export function extractionCheck(items: ExtractionItem[]) {
  const summary = summarize(items);
  const { accepted, pending, rejected } = summary.decisions;
  return {
    name: 'extraction',
    status: pending > 0 ? ('pending' as const) : ('passed' as const),
    message: `${summary.items} item(s): ${accepted} accepted, ${pending} pending, ${rejected} rejected.`,
  };
}

// ------------------------------------------------------------ validation

function invalid(pointer: string, message: string) {
  return apiErrorFromProblem({
    type: 'urn:lorebase:problem:validation-failed',
    title: 'Request validation failed',
    status: 422,
    code: 'validation-failed',
    detail: 'One or more request members are invalid.',
    errors: [{ pointer, message, code: 'invalid' }],
  });
}

const kindOf = (data: BranchData, name: string | null): Kind | undefined =>
  data.schema.kinds.find((k) => k.api_name === name);

const attributeOf = (kind: Kind | undefined, name: string): Attribute | undefined =>
  Object.values(kind?.attributes ?? {}).find((a) => a.api_name === name);

const relationOf = (data: BranchData, name: string | null): Relation | undefined =>
  data.schema.relations.find((r) => r.api_name === name);

const ALLOWED: Record<ExtractionItem['type'], (keyof ItemEdits)[]> = {
  record: ['values'],
  value: ['value', 'key'],
  link: ['from', 'to', 'values'],
};

function checkEdits(
  data: BranchData,
  item: ExtractionItem,
  edits: ItemEdits,
  pointer: string,
): void {
  const unknown = Object.keys(edits).filter((name) => !ALLOWED[item.type].includes(name as never));
  if (unknown.length > 0) {
    throw invalid(
      pointer,
      `${item.type} items accept edits of ${ALLOWED[item.type].join(', ')}, not ${unknown.join(', ')}`,
    );
  }
  const kind = kindOf(data, item.kind);
  for (const [name, value] of Object.entries(edits.values ?? {})) {
    if (item.type === 'link') continue;
    const attribute = attributeOf(kind, name);
    if (attribute === undefined) throw invalid(pointer, `unknown Attribute ${name}`);
    const problem = validateValue(attribute, value);
    if (problem !== null) throw invalid(`${pointer}/values/${name}`, `${name}: ${problem}`);
  }
  if (edits.value !== undefined && item.attribute !== null) {
    const attribute = attributeOf(kind, item.attribute);
    const problem = attribute === undefined ? null : validateValue(attribute, edits.value);
    if (problem !== null) throw invalid(`${pointer}/value`, `${item.attribute}: ${problem}`);
  }
  for (const name of ['key', 'from', 'to'] as const) {
    const given = edits[name];
    if (given !== undefined && given.trim() === '')
      throw invalid(`${pointer}/${name}`, 'a key is required');
  }
}

// -------------------------------------------------------- materialization

interface Built {
  records: StoredRecord[];
  links: StoredLink[];
  applied: number;
  changes: number;
  skipped: Record<string, string>;
  issues: ProblemIssue[];
}

/** The branch content for the accepted items laid over the base Branch. */
function build(base: BranchData, items: ExtractionItem[]): Built {
  const records = structuredClone(base.records);
  const links = structuredClone(base.links);
  const skipped: Record<string, string> = {};
  const issues: ProblemIssue[] = [];
  let applied = 0;
  let changes = 0;
  const exists = (kind: string, key: string) =>
    records.some((r) => r.kind === kind && r.key === key);
  const added: { item: ExtractionItem; ref: string }[] = [];

  const accepted = items
    .filter((item) => item.decision === 'accepted')
    .sort((a, b) => a.seq - b.seq);

  for (const item of accepted.filter((i) => i.type === 'record')) {
    const { kind, key } = item;
    if (kind === null || key === null) continue;
    if (exists(kind, key)) {
      skipped[item.id] = `${kind} ${key} exists on the branch already.`;
      continue;
    }
    const kindDef = kindOf(base, kind);
    const missing = Object.values(kindDef?.attributes ?? {}).filter(
      (a) => a.required && (item.values[a.api_name] ?? null) === null,
    );
    for (const attribute of missing) {
      issues.push({
        code: 'value.required',
        message: `${kind} ${key} has no value for the required Attribute ${attribute.api_name}.`,
        path: ['items', item.id],
      });
    }
    records.push({ kind, key, values: { ...item.values }, derived: {} });
    added.push({ item, ref: refOf(kind, key) });
    applied += 1;
    changes += 1;
  }

  for (const item of accepted.filter((i) => i.type === 'value')) {
    const record = records.find((r) => r.kind === item.kind && r.key === item.key);
    if (record === undefined || item.attribute === null) {
      skipped[item.id] = `${item.kind ?? ''} ${item.key ?? ''} is not on the branch.`.trim();
      continue;
    }
    if (JSON.stringify(record.values[item.attribute]) !== JSON.stringify(item.value)) changes += 1;
    record.values[item.attribute] = item.value;
    applied += 1;
  }

  for (const item of accepted.filter((i) => i.type === 'link')) {
    const relation = relationOf(base, item.relation);
    const from = base.schema.kinds.find((k) => k.id === relation?.from_kind);
    const to = base.schema.kinds.find((k) => k.id === relation?.to_kind);
    if (relation === undefined || from === undefined || to === undefined) continue;
    if (item.from === null || item.to === null) continue;
    const missingEnd = !exists(from.api_name, item.from)
      ? refOf(from.api_name, item.from)
      : !exists(to.api_name, item.to)
        ? refOf(to.api_name, item.to)
        : null;
    if (missingEnd !== null) {
      skipped[item.id] = `${missingEnd} is not on the branch; accept its Record too.`;
      continue;
    }
    const fromRef = refOf(from.api_name, item.from);
    const toRef = refOf(to.api_name, item.to);
    if (
      links.some((l) => l.relation === relation.api_name && l.from === fromRef && l.to === toRef)
    ) {
      skipped[item.id] = 'The Link exists already.';
      continue;
    }
    links.push({
      relation: relation.api_name,
      from: fromRef,
      to: toRef,
      values: { ...item.values },
    });
    applied += 1;
    changes += 1;
  }

  // A new Record needs the Links its Kind requires (a Contract its with_supplier Link).
  for (const { item, ref } of added) {
    const kindDef = kindOf(base, item.kind);
    for (const relation of base.schema.relations) {
      if (!relation.required_from || relation.from_kind !== kindDef?.id) continue;
      if (!links.some((l) => l.relation === relation.api_name && l.from === ref)) {
        issues.push({
          code: 'link.required',
          message: `${ref} needs a ${relation.api_name} Link; accept the Record and its Link together.`,
          path: ['items', item.id],
        });
      }
    }
  }
  return { records, links, applied, changes, skipped, issues };
}

/**
 * Lays the accepted items over the base and, when anything changed, puts one extraction
 * Revision on top of the base history. Returns what Keeper reports as `materialized`.
 */
export function materialize(
  state: ProposalState,
  branch: BranchData,
  domain: string,
  built: Built,
): Materialized {
  const extraction = state.extraction;
  if (extraction === undefined) throw new Error('Not an extraction Proposal.');
  branch.records = built.records;
  branch.links = built.links;
  extraction.skipped = built.skipped;
  const parent = extraction.base.log[0]?.id ?? null;
  if (built.changes === 0) {
    branch.log = [...extraction.base.log];
    return { revision: null, changes: 0, applied: built.applied, skipped: built.skipped };
  }
  extraction.revisions += 1;
  const id = `x${state.detail.id.padStart(4, '0')}${extraction.revisions.toString(16).padStart(7, '0')}`;
  const created = buildRevision(
    domain,
    id,
    parent,
    `Extract ${state.detail.title}`,
    new Date().toISOString(),
    'extraction',
    { author: 'svc_scribe', changes: built.changes, originRef: state.detail.id },
  );
  branch.log = [created, ...extraction.base.log];
  return { revision: id, changes: built.changes, applied: built.applied, skipped: built.skipped };
}

/** The branch as the seed items would have it, for Proposals seeded with accepted items. */
export function seedBranch(state: ProposalState, domain: string, branch: BranchData): void {
  materialize(
    state,
    branch,
    domain,
    build(state.extraction?.base ?? branch, state.items.map(view)),
  );
}

/** Applies a decide request; on a refusal (422) nothing changes. */
export function decide(
  state: ProposalState,
  branch: BranchData,
  domain: string,
  input: DecideItemsInput,
): DecideItemsResult {
  const extraction = state.extraction;
  if (extraction === undefined) {
    throw apiErrorFromProblem({
      type: 'urn:lorebase:problem:conflict',
      title: 'Not an extraction Proposal',
      status: 409,
      detail: `Proposal ${state.detail.id} has no extraction items.`,
    });
  }
  if (state.detail.status !== 'open') {
    throw apiErrorFromProblem({
      type: 'urn:lorebase:problem:conflict',
      title: 'Proposal not open',
      status: 409,
      detail: 'Only open Proposals take decisions.',
    });
  }
  const decisions = input.decisions ?? [];
  if (decisions.length === 0 && (input.where === undefined || input.decision === undefined)) {
    throw invalid('/body', 'give `decisions`, or `where` with `decision`');
  }
  const next = structuredClone(state.items);
  const at = new Date().toISOString();
  const touched = new Set<string>();
  const setDecision = (item: ExtractionItem, decision: ItemDecision) => {
    item.decision = decision;
    item.decided_by = decision === 'pending' ? null : ACTOR;
    item.decided_at = decision === 'pending' ? null : at;
    touched.add(item.id);
  };
  decisions.forEach((entry, index) => {
    const item = next.find((i) => i.id === entry.item);
    const pointer = `/body/decisions/${index.toString()}`;
    if (item === undefined) throw invalid(pointer, `unknown item ${entry.item}`);
    if (entry.edits !== undefined) {
      checkEdits(extraction.base, item, entry.edits, `${pointer}/edits`);
      item.edits = Object.keys(entry.edits).length === 0 ? null : entry.edits;
    }
    setDecision(item, entry.decision);
  });
  if (input.where !== undefined && input.decision !== undefined) {
    for (const item of next) {
      if (!touched.has(item.id) && matches(view(item), input.where)) {
        setDecision(item, input.decision);
      }
    }
  }

  const built = build(extraction.base, next.map(view));
  if (built.issues.length > 0) {
    throw apiErrorFromProblem({
      type: 'urn:lorebase:problem:validation-failed',
      title: 'The accepted items do not form a valid branch',
      status: 422,
      code: 'validation-failed',
      detail: 'The accepted items do not form a valid branch; nothing changed.',
      issues: built.issues,
    });
  }
  state.items = next;
  const materialized = materialize(state, branch, domain, built);
  const check = extractionCheck(state.items);
  state.detail.checks = [...state.detail.checks.filter((c) => c.name !== 'extraction'), check];
  const summary: ExtractionSummary = {
    ...summarize(state.items),
    applied: materialized.applied,
    skipped: materialized.skipped,
    revision: materialized.revision,
  };
  return {
    proposal_id: state.detail.id,
    title: state.detail.title,
    checks: { extraction: summary },
    items: state.items
      .filter((item) => touched.has(item.id))
      .sort((a, b) => a.seq - b.seq)
      .map((item) => withReason(state, view(item))),
    materialized,
  };
}
