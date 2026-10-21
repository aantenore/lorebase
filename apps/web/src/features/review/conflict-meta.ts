import { GROUPED_KINDS } from '@/api/conflict-rules';
import type { Conflict, ConflictCode, JsonValue, ResolutionChoice, Schema } from '@/api/types';
import { parseAttributeType, type ValueSpec } from '@/api/values';

/** Display names of the Conflict codes (design 6.9). */
export const CODE_LABEL: Record<ConflictCode, string> = {
  DV1: 'Attribute value',
  DV2: 'Delete or modify',
  DV3: 'Add or add',
  SC1: 'Schema property',
  SC2: 'Schema delete or modify',
  SC3: 'Naming collision',
  XS1: 'Orphaned data',
  XS2: 'Orphaned value',
  XS3: 'Type coercion',
  XS4: 'Missing value',
  XS5: 'Constraint',
  XS6: 'Cardinality',
  XS7: 'Required relation',
  XS8: 'Uniqueness',
  XS9: 'Dangling Link',
  XS10: 'Trait conformance',
  XS11: 'Broken reference',
};

export const isGrouped = (conflict: Conflict) => GROUPED_KINDS.has(conflict.kind);

/** Numeric order of codes, so lists read DV1, DV2, ..., SC1, ..., XS1, ... XS11. */
const CLASS_ORDER = { DV: 0, SC: 1, XS: 2 } as const;

export function compareCodes(a: ConflictCode, b: ConflictCode): number {
  const order = (code: ConflictCode) => CLASS_ORDER[code.slice(0, 2) as keyof typeof CLASS_ORDER];
  const number = (code: ConflictCode) => Number(code.slice(2));
  return order(a) - order(b) || number(a) - number(b);
}

export function choiceLabel(conflict: Conflict, choice: ResolutionChoice): string {
  switch (choice) {
    case 'ours':
    case 'theirs': {
      const absent = conflict[choice] === null;
      if (conflict.kind === 'DV2' || conflict.kind === 'SC2') {
        return absent ? `Keep deleted (${choice})` : `Keep modified (${choice})`;
      }
      return absent ? `Take ${choice} (absent)` : `Take ${choice}`;
    }
    case 'base':
      return 'Revert to base';
    case 'custom':
      return 'Custom value';
    case 'drop':
      return conflict.kind === 'XS9' ? 'Drop the Links' : 'Drop the data';
    case 'restore':
      return conflict.kind === 'XS9' ? 'Restore the Record' : 'Restore what was removed';
    case 'relax':
      return 'Relax the rule';
  }
}

export function choiceHint(conflict: Conflict, choice: ResolutionChoice): string | undefined {
  if (choice !== 'relax') return undefined;
  const relaxed = conflict.details['relaxed'];
  return relaxed === undefined ? undefined : `Sets the rule to ${JSON.stringify(relaxed)}.`;
}

/** Display names for element ids, taken from the schema the Conflicts are shown against. */
export type Names = ReadonlyMap<string, string>;

export function buildNames(schema: Schema | undefined): Names {
  const names = new Map<string, string>();
  if (schema === undefined) return names;
  const attributes = (owner: { attributes: Record<string, { api_name: string }> }) => {
    for (const [id, attribute] of Object.entries(owner.attributes)) {
      names.set(id, attribute.api_name);
    }
  };
  for (const kind of schema.kinds) {
    names.set(kind.id, kind.api_name);
    attributes(kind);
  }
  for (const relation of schema.relations) {
    names.set(relation.id, relation.api_name);
    attributes(relation);
  }
  for (const element of [
    ...schema.traits,
    ...schema.operations,
    ...schema.derivations,
    ...schema.sources,
  ]) {
    names.set(element.id, element.api_name);
  }
  return names;
}

/** Replace ids inside a dotted path with the names they stand for. */
export function namePath(path: string, names: Names): string {
  return path
    .split('.')
    .map((part) => names.get(part) ?? part)
    .join('.');
}

/**
 * The Kind or Relation a partition stands for. Keeper names a data partition `k:{kind id}` or
 * `r:{relation id}`; a plain name (the mock) and `schema` stand for themselves.
 */
export function partitionName(partition: string, names: Names): string {
  const match = /^[kr]:(.+)$/.exec(partition);
  const id = match?.[1];
  return id === undefined ? partition : (names.get(id) ?? id);
}

const ELEMENT_ID = /\b(?:knd|att|rel|trt|opr|drv|src)_[0-9A-Z]{26}\b/g;

/** A Keeper message with the element ids it mentions replaced by their names. */
export function messageOf(conflict: Conflict, names: Names): string {
  return conflict.message.replace(ELEMENT_ID, (id) => names.get(id) ?? id);
}

/** One line locating a Conflict: `Supplier/S-1003 · tier` or `Supplier · attributes.risk_score.description`. */
export function locationOf(conflict: Conflict, names: Names): string {
  const key = conflict.key === null ? null : (names.get(conflict.key) ?? conflict.key);
  const partition = partitionName(conflict.partition, names);
  const place =
    conflict.partition === 'schema'
      ? (key ?? 'schema')
      : key === null
        ? partition
        : `${partition}/${key}`;
  return conflict.attribute === null ? place : `${place} · ${namePath(conflict.attribute, names)}`;
}

/** The type the sides of a schema property have, for the custom value editor. */
export function inferSpec(values: JsonValue[]): ValueSpec {
  const sample = values.find((v) => v !== null);
  if (typeof sample === 'number') {
    return { type: Number.isInteger(sample) ? 'integer' : 'float' };
  }
  if (typeof sample === 'boolean') return { type: 'boolean' };
  if (typeof sample === 'string') return { type: sample.length > 60 ? 'text' : 'string' };
  return { type: 'json' };
}

/**
 * What a custom value of a Conflict must be: the Attribute type for data Conflicts, the
 * type of the competing values for schema properties, a name for collisions, a document
 * for whole Records and elements. `null` when the editor should take any JSON.
 */
export function specOf(conflict: Conflict, schema: Schema | undefined): ValueSpec | null {
  const names = buildNames(schema);
  if (conflict.kind === 'SC3') {
    return { type: 'string', required: true, constraints: { pattern: '^[A-Za-z][A-Za-z0-9_]*$' } };
  }
  if (conflict.kind === 'DV2' || conflict.kind === 'SC2') return { type: 'json' };
  if (conflict.kind.startsWith('SC')) {
    return inferSpec([conflict.base, conflict.ours, conflict.theirs]);
  }
  const partition = partitionName(conflict.partition, names);
  const kind = schema?.kinds.find((k) => k.api_name === partition);
  const attribute = kind?.attributes[conflict.attribute ?? ''];
  if (attribute === undefined) return null;
  const spec: ValueSpec = {
    type: attribute.type,
    required: attribute.required,
    constraints: attribute.constraints,
  };
  return parseAttributeType(spec.type) === null ? null : spec;
}
