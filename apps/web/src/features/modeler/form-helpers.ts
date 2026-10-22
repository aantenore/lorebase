import { renamePlaceholder } from '@/api/embedding-template';
import type { SchemaIssue } from '@/api/schema-rules';
import { SCALAR_TYPES } from '@/api/values';
import type {
  AttributeDocument,
  KindDocument,
  RelationDocument,
  SchemaDocument,
  TraitDocument,
  TypeDocument,
} from './document';

/** Edits a copy of the document; the editor commits the copy to both views. */
export type Edit = (change: (draft: SchemaDocument) => void) => void;

export type ElementIds = (prefix: string, key: string) => string;

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export const asList = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

/** `list` with the item at `from` moved to `to`; the same list when either is out of range. */
export function moveItem<T>(list: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item as T);
  return next;
}

export const asText = (value: unknown): string => (typeof value === 'string' ? value : '');

// ------------------------------------------------------------------ types

/** The select value of a type: `string`, `enum`, `array<enum>`, `ref(Kind)`. */
export function typeLabel(type: TypeDocument | undefined): string {
  if (type === undefined) return 'string';
  if (typeof type === 'string') return type;
  if ('enum' in type) return 'enum';
  return typeof type.array === 'string' ? `array<${type.array}>` : 'array<enum>';
}

export function enumValuesOf(type: TypeDocument | undefined): string[] {
  if (type === undefined || typeof type === 'string') return [];
  if ('enum' in type) return type.enum;
  return typeof type.array === 'string' ? [] : type.array.enum;
}

export function typeFromLabel(label: string, values: string[]): TypeDocument {
  if (label === 'enum') return { enum: values };
  if (label === 'array<enum>') return { array: { enum: values } };
  const array = /^array<(.+)>$/.exec(label);
  return array?.[1] === undefined ? label : { array: array[1] };
}

/** Types offered for an Attribute: scalars, lists of scalars, references to Kinds. */
export function typeOptions(kindNames: string[], current: string): string[] {
  const options = [
    ...SCALAR_TYPES.map(String),
    ...SCALAR_TYPES.map((t) => `array<${t}>`),
    ...kindNames.map((k) => `ref(${k})`),
  ];
  return options.includes(current) ? options : [...options, current];
}

export function listOfText(text: string): string[] {
  return text
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part !== '');
}

// ------------------------------------------------------------------ issues

export function errorAt(issues: SchemaIssue[], path: string): string | undefined {
  const found = issues.filter((issue) => issue.path === path).map((issue) => issue.message);
  return found.length === 0 ? undefined : found.join(' ');
}

/** Errors at any of `paths`, joined. */
export function errorsAt(issues: SchemaIssue[], ...paths: string[]): string | undefined {
  const found = paths.map((p) => errorAt(issues, p)).filter((e) => e !== undefined);
  return found.length === 0 ? undefined : found.join(' ');
}

/** Whether any issue sits at or under `path`. */
export function hasIssuesUnder(issues: SchemaIssue[], path: string): boolean {
  return issues.some((i) => i.path === path || i.path.startsWith(`${path}/`));
}

// ------------------------------------------------------------------ records

/** `record` with the key `from` renamed to `to`, same position. */
export function renameKey<T>(
  record: Record<string, T>,
  from: string,
  to: string,
): Record<string, T> {
  return Object.fromEntries(Object.entries(record).map(([k, v]) => [k === from ? to : k, v]));
}

const withRef = (
  type: TypeDocument | undefined,
  from: string,
  to: string,
): TypeDocument | undefined => (type === `ref(${from})` ? `ref(${to})` : type);

function eachAttribute(draft: SchemaDocument, visit: (attribute: AttributeDocument) => void) {
  for (const owner of [
    ...Object.values(draft.kinds ?? {}),
    ...Object.values(draft.relations ?? {}),
  ]) {
    for (const attribute of Object.values(owner.attributes ?? {})) visit(attribute);
  }
}

/** Rename a Kind and every reference to it: Relations, `ref(...)` types, Derivations, Sources. */
export function renameKind(draft: SchemaDocument, from: string, to: string) {
  if (draft.kinds === undefined || from === to) return;
  draft.kinds = renameKey(draft.kinds, from, to);
  for (const relation of Object.values(draft.relations ?? {})) {
    if (relation.from_kind === from) relation.from_kind = to;
    if (relation.to_kind === from) relation.to_kind = to;
  }
  eachAttribute(draft, (attribute) => {
    attribute.type = withRef(attribute.type, from, to) ?? attribute.type;
  });
  for (const derivation of Object.values(draft.derivations ?? {})) {
    const definition = derivation['definition'];
    if (isRecord(definition) && definition['kind'] === from) definition['kind'] = to;
  }
  for (const source of Object.values(draft.sources ?? {})) {
    if (source['target_kind'] === from) source['target_kind'] = to;
  }
  for (const operation of Object.values(draft.operations ?? {})) {
    for (const parameter of asList(operation['parameters'])) {
      if (isRecord(parameter) && parameter['type'] === `ref(${from})`) {
        parameter['type'] = `ref(${to})`;
      }
    }
    for (const step of asList(operation['steps'])) {
      if (isRecord(step) && step['kind'] === from) step['kind'] = to;
    }
  }
}

/** Rename an Attribute of a Kind and the references inside the Kind and its Relations. */
export function renameAttribute(draft: SchemaDocument, kind: string, from: string, to: string) {
  const owner = draft.kinds?.[kind];
  if (owner?.attributes === undefined || from === to) return;
  owner.attributes = renameKey(owner.attributes, from, to);
  owner.primary_key = owner.primary_key?.map((n) => (n === from ? to : n));
  if (owner.title_attribute === from) owner.title_attribute = to;
  if (owner.search?.embedding_template !== undefined) {
    owner.search.embedding_template = renamePlaceholder(owner.search.embedding_template, from, to);
  }
  for (const implementation of owner.traits ?? []) {
    for (const [traitAttribute, own] of Object.entries(implementation.attribute_map)) {
      if (own === from) implementation.attribute_map[traitAttribute] = to;
    }
  }
  for (const relation of Object.values(draft.relations ?? {})) {
    if (relation.from_kind === kind && relation.foreign_key_attribute === from) {
      relation.foreign_key_attribute = to;
    }
  }
  for (const derivation of Object.values(draft.derivations ?? {})) {
    const definition = derivation['definition'];
    if (isRecord(definition) && definition['kind'] === kind && definition['attribute'] === from) {
      definition['attribute'] = to;
    }
  }
  for (const source of Object.values(draft.sources ?? {})) {
    const mapping = source['attribute_mapping'];
    if (source['target_kind'] === kind && isRecord(mapping)) {
      source['attribute_mapping'] = renameKey(mapping, from, to);
    }
  }
  for (const operation of Object.values(draft.operations ?? {})) {
    for (const step of asList(operation['steps'])) {
      if (isRecord(step) && step['kind'] === kind && isRecord(step['values'])) {
        step['values'] = renameKey(step['values'], from, to);
      }
    }
  }
}

// ------------------------------------------------------------------ additions

function uniqueName(existing: Record<string, unknown> | undefined, stem: string): string {
  const names = new Set(Object.keys(existing ?? {}));
  if (!names.has(stem)) return stem;
  let n = 2;
  while (names.has(`${stem}_${n}`)) n += 1;
  return `${stem}_${n}`;
}

export function newAttribute(
  attributes: Record<string, AttributeDocument> | undefined,
  ids: ElementIds,
  scope: string,
): [string, AttributeDocument] {
  const name = uniqueName(attributes, 'new_attribute');
  return [name, { id: ids('att', `${scope}/${name}`), type: 'string' }];
}

export function addKind(draft: SchemaDocument, name: string, ids: ElementIds) {
  const attribute: AttributeDocument = {
    id: ids('att', `kinds/${name}/name`),
    type: 'string',
    required: true,
  };
  const kind: KindDocument = {
    id: ids('knd', `kinds/${name}`),
    primary_key: ['name'],
    title_attribute: 'name',
    attributes: { name: attribute },
  };
  draft.kinds = { ...draft.kinds, [name]: kind };
}

export function addRelation(draft: SchemaDocument, name: string, ids: ElementIds) {
  const kinds = Object.keys(draft.kinds ?? {});
  const relation: RelationDocument = {
    id: ids('rel', `relations/${name}`),
    inverse_api_name: `${name}_inverse`,
    from_kind: kinds[0] ?? '',
    to_kind: kinds[1] ?? kinds[0] ?? '',
    cardinality: 'many_to_one',
  };
  draft.relations = { ...draft.relations, [name]: relation };
}

export function addTrait(draft: SchemaDocument, name: string, ids: ElementIds) {
  const trait: TraitDocument = {
    id: ids('trt', `traits/${name}`),
    attributes: { value: { id: ids('att', `traits/${name}/value`), type: 'string' } },
  };
  draft.traits = { ...draft.traits, [name]: trait };
}

export function removeKey<T>(
  record: Record<string, T> | undefined,
  name: string,
): Record<string, T> {
  return Object.fromEntries(Object.entries(record ?? {}).filter(([key]) => key !== name));
}
