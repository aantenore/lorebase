/**
 * Merge attempts of the mock adapter (design 6.8, 6.10).
 *
 * The Conflicts of a Proposal are seeded, not recomputed from the Branches. What is real is
 * the part Review depends on: Resolutions are checked against the Conflict (choice, shape
 * and, at merge time, value against the merged schema), applied to a copy of the source
 * state, and the Conflicts that remain are reported with the reason a Resolution was refused.
 */
import { GROUPED_KINDS } from '../conflict-rules';
import { problem } from '../errors';
import type { Conflict, JsonValue, MergeAttempt, Resolution, Kind, RevisionId } from '../types';
import { validateValue, type ValueSpec } from '../values';
import {
  headOf,
  refOf,
  splitRef,
  type BranchData,
  type ProposalState,
  type StoredRecord,
} from './state';

type Json = Record<string, unknown>;

/** The latest Revision that both Branches contain. */
export function mergeBaseOf(target: BranchData, source: BranchData): RevisionId {
  const own = new Set(target.log.map((r) => r.id));
  const common = source.log.find((r) => own.has(r.id));
  return common?.id ?? target.log[target.log.length - 1]?.id ?? headOf(target);
}

export function attemptOf(
  proposal: string,
  state: ProposalState,
  target: BranchData,
  source: BranchData,
): MergeAttempt {
  const heads = state.mergedHeads ?? {
    base: mergeBaseOf(target, source),
    ours: headOf(target),
    theirs: headOf(source),
  };
  return structuredClone({
    proposal,
    ...heads,
    conflicts: state.conflicts,
    resolutions: state.resolutions,
    auto_resolutions: state.autoResolutions,
  });
}

// ------------------------------------------------------------- checking

const invalid = (detail: string) =>
  problem(422, 'Invalid resolution', detail, 'invalid_resolution');

/** The reason a Resolution cannot apply to a Conflict at all, or `null`. Checked on store. */
export function shapeProblem(conflict: Conflict, resolution: Resolution): string | null {
  if (!conflict.choices.includes(resolution.choice)) {
    return `${resolution.choice} is not a choice for ${conflict.kind}.`;
  }
  const carriesValue = resolution.value !== undefined || resolution.values !== undefined;
  if (resolution.choice !== 'custom') {
    return carriesValue ? 'Only a custom resolution carries a value.' : null;
  }
  if (!carriesValue) return 'A custom resolution needs a value.';
  if (GROUPED_KINDS.has(conflict.kind) && resolution.value === undefined) {
    const missing = conflict.sample.filter((member) => resolution.values?.[member] === undefined);
    if (missing.length > 0) return `No value for ${missing.join(', ')}.`;
  }
  return null;
}

export function assertShape(conflict: Conflict, resolution: Resolution) {
  const reason = shapeProblem(conflict, resolution);
  if (reason !== null) throw invalid(reason);
}

function valueFor(resolution: Resolution, member: string | null): JsonValue | undefined {
  if (member !== null && resolution.values?.[member] !== undefined)
    return resolution.values[member];
  return resolution.value;
}

function attributeSpec(schema: BranchData['schema'], conflict: Conflict): ValueSpec | null {
  const kind = schema.kinds.find((k) => k.api_name === conflict.partition);
  const attribute = kind?.attributes[conflict.attribute ?? ''];
  if (attribute === undefined) return null;
  return { type: attribute.type, required: attribute.required, constraints: attribute.constraints };
}

function recordProblem(schema: BranchData['schema'], kindName: string, value: JsonValue) {
  if (value === null) return null;
  if (typeof value !== 'object' || Array.isArray(value))
    return 'A Record is an object of Attribute values.';
  const kind = schema.kinds.find((k) => k.api_name === kindName);
  for (const attribute of Object.values(kind?.attributes ?? {})) {
    const reason = validateValue(
      { type: attribute.type, required: attribute.required, constraints: attribute.constraints },
      value[attribute.api_name],
    );
    if (reason !== null) return `${attribute.api_name}: ${reason}`;
  }
  return null;
}

/** The reason a custom value does not fit the merged schema, or `null`. Checked on merge. */
export function valueProblem(
  schema: BranchData['schema'],
  conflict: Conflict,
  resolution: Resolution,
): string | null {
  if (resolution.choice !== 'custom') return null;
  if (conflict.kind === 'DV2') {
    return recordProblem(schema, conflict.partition, resolution.value ?? null);
  }
  if (conflict.kind === 'SC3') {
    const name = resolution.value;
    return typeof name === 'string' && /^[A-Za-z][A-Za-z0-9_]*$/.test(name)
      ? null
      : 'A name starts with a letter and has letters, digits and underscores.';
  }
  if (conflict.kind === 'SC1' || conflict.kind === 'SC2') {
    const reference = [conflict.base, conflict.ours, conflict.theirs].find((v) => v !== null);
    const value = resolution.value ?? null;
    return reference !== undefined && value !== null && typeof value !== typeof reference
      ? `Must be a ${typeof reference}.`
      : null;
  }
  const spec = attributeSpec(schema, conflict);
  if (spec === null) return null;
  const members = GROUPED_KINDS.has(conflict.kind) ? conflict.sample : [null];
  for (const member of members) {
    const reason = validateValue(spec, valueFor(resolution, member));
    if (reason !== null) return member === null ? reason : `${member}: ${reason}`;
  }
  return null;
}

// ------------------------------------------------------------- applying

const COLLECTIONS = [
  'kinds',
  'relations',
  'traits',
  'operations',
  'derivations',
  'sources',
] as const satisfies readonly string[];

function elementById(schema: BranchData['schema'], id: string): Json | undefined {
  for (const collection of COLLECTIONS) {
    const found = (schema[collection] as { id: string }[]).find((e) => e.id === id);
    if (found !== undefined) return found;
  }
  return undefined;
}

function removeElement(schema: BranchData['schema'], id: string) {
  for (const collection of COLLECTIONS) {
    const list = schema[collection] as { id: string }[];
    const index = list.findIndex((e) => e.id === id);
    if (index >= 0) list.splice(index, 1);
  }
}

function setPath(root: Json, path: readonly (string | number)[], value: unknown) {
  let node = root;
  for (const part of path.slice(0, -1)) {
    const next = node[part];
    if (typeof next !== 'object' || next === null) return;
    node = next as Json;
  }
  const last = path[path.length - 1];
  if (last === undefined) return;
  if (value === null || value === undefined) {
    delete node[last];
  } else {
    node[last] = structuredClone(value);
  }
}

function pathOf(conflict: Conflict): (string | number)[] {
  const raw = conflict.details['path'];
  if (Array.isArray(raw)) return raw.filter((p): p is string | number => typeof p !== 'object');
  return (conflict.attribute ?? '').split('.').filter((part) => part !== '');
}

function choiceValue(conflict: Conflict, resolution: Resolution): JsonValue {
  switch (resolution.choice) {
    case 'ours':
    case 'theirs':
    case 'base':
      return conflict[resolution.choice];
    default:
      return resolution.value ?? null;
  }
}

function attributeName(schema: BranchData['schema'], conflict: Conflict): string | null {
  const kind = schema.kinds.find((k) => k.api_name === conflict.partition);
  return kind?.attributes[conflict.attribute ?? '']?.api_name ?? null;
}

function findRecord(data: BranchData, kind: string, key: string): StoredRecord | undefined {
  return data.records.find((r) => r.kind === kind && r.key === key);
}

function setRecordValue(data: BranchData, kind: string, key: string, name: string, value: unknown) {
  const found = findRecord(data, kind, key);
  if (found === undefined) return;
  if (value === null || value === undefined) {
    delete found.values[name];
  } else {
    found.values[name] = value;
  }
}

function upsertRecord(data: BranchData, kind: string, key: string, values: Json) {
  const existing = findRecord(data, kind, key);
  if (existing === undefined) data.records.push({ kind, key, values, derived: {} });
  else existing.values = values;
}

interface LinkRef {
  relation: string;
  from: string;
  to: string;
}

function linkRefs(conflict: Conflict): LinkRef[] {
  const raw = conflict.details['links'];
  return Array.isArray(raw) ? (raw as unknown as LinkRef[]) : [];
}

function restoreRecord(merged: BranchData, target: BranchData, ref: string) {
  const { kind, key } = splitRef(ref);
  const original = findRecord(target, kind, key);
  if (original !== undefined && findRecord(merged, kind, key) === undefined) {
    merged.records.push(structuredClone(original));
  }
}

function applyRelax(merged: BranchData, conflict: Conflict) {
  const element = conflict.details['element'];
  const owner = typeof element === 'string' ? elementById(merged.schema, element) : undefined;
  if (owner !== undefined) setPath(owner, pathOf(conflict), conflict.details['relaxed'] ?? null);
}

function applySchemaConflict(merged: BranchData, conflict: Conflict, resolution: Resolution) {
  const id = conflict.key ?? '';
  const chosen = choiceValue(conflict, resolution);
  if (conflict.kind === 'SC2') {
    if (chosen === null) removeElement(merged.schema, id);
    else {
      removeElement(merged.schema, id);
      merged.schema.kinds.push(structuredClone(chosen) as unknown as Kind);
    }
    return;
  }
  const element = elementById(merged.schema, id);
  if (element !== undefined) setPath(element, pathOf(conflict), chosen);
}

function applyGrouped(
  merged: BranchData,
  target: BranchData,
  conflict: Conflict,
  resolution: Resolution,
) {
  const name = attributeName(merged.schema, conflict);
  if (conflict.kind === 'XS9') {
    if (resolution.choice === 'restore') {
      const record = conflict.details['record'];
      if (typeof record === 'string') restoreRecord(merged, target, record);
      for (const link of linkRefs(conflict)) {
        const exists = merged.links.some(
          (l) => l.relation === link.relation && l.from === link.from && l.to === link.to,
        );
        if (!exists) merged.links.push({ ...link, values: {} });
      }
    } else {
      merged.links = merged.links.filter(
        (l) =>
          !linkRefs(conflict).some(
            (x) => x.relation === l.relation && x.from === l.from && x.to === l.to,
          ),
      );
    }
    return;
  }
  if (resolution.choice === 'custom' && name !== null) {
    for (const member of conflict.sample) {
      setRecordValue(merged, conflict.partition, member, name, valueFor(resolution, member));
    }
  }
}

function applyOne(
  merged: BranchData,
  target: BranchData,
  conflict: Conflict,
  resolution: Resolution,
) {
  if (resolution.choice === 'relax') {
    applyRelax(merged, conflict);
    return;
  }
  if (conflict.kind.startsWith('SC')) {
    applySchemaConflict(merged, conflict, resolution);
    return;
  }
  if (GROUPED_KINDS.has(conflict.kind)) {
    applyGrouped(merged, target, conflict, resolution);
    return;
  }
  const key = conflict.key ?? '';
  if (conflict.kind === 'DV2') {
    const chosen = choiceValue(conflict, resolution);
    if (chosen === null) {
      merged.records = merged.records.filter(
        (r) => refOf(r.kind, r.key) !== refOf(conflict.partition, key),
      );
    } else {
      upsertRecord(merged, conflict.partition, key, structuredClone(chosen) as Json);
    }
    return;
  }
  const name = attributeName(merged.schema, conflict);
  if (name !== null) {
    setRecordValue(merged, conflict.partition, key, name, choiceValue(conflict, resolution));
  }
}

export interface MergeRun {
  /** The merged state, or `null` while Conflicts remain. */
  merged: BranchData | null;
  remaining: Conflict[];
}

const isSchemaPhase = (conflict: Conflict, resolution: Resolution | undefined) =>
  conflict.kind.startsWith('SC') || resolution?.choice === 'relax';

/**
 * Apply the stored Resolutions to a copy of the source state, schema changes first so
 * custom values are checked against the merged schema. A refused Resolution is dropped and
 * its reason kept on the Conflict; the Conflict stays open.
 */
export function runMerge(state: ProposalState, target: BranchData, source: BranchData): MergeRun {
  const merged = structuredClone(source);
  const stored = new Map(state.resolutions.map((r) => [r.fingerprint, r]));
  const ordered = [...state.conflicts].sort(
    (a, b) =>
      Number(isSchemaPhase(b, stored.get(b.fingerprint))) -
      Number(isSchemaPhase(a, stored.get(a.fingerprint))),
  );
  const remaining: Conflict[] = [];
  for (const conflict of ordered) {
    delete conflict.details['rejected'];
    const resolution = stored.get(conflict.fingerprint);
    if (resolution === undefined) {
      remaining.push(conflict);
      continue;
    }
    const reason = valueProblem(merged.schema, conflict, resolution);
    if (reason !== null) {
      conflict.details['rejected'] = `${resolution.choice} refused: ${reason}`;
      state.resolutions = state.resolutions.filter((r) => r.fingerprint !== conflict.fingerprint);
      remaining.push(conflict);
      continue;
    }
    applyOne(merged, target, conflict, resolution);
  }
  return { merged: remaining.length === 0 ? merged : null, remaining };
}
