/**
 * Edits of Operations, Derivations and Sources in the schema document. Each helper changes
 * only the keys it is about: keys the Modeler does not model stay where they are, so a form
 * edit never drops what the YAML says.
 */
import { SOURCE_TYPE_FIELDS, STEP_FIELDS, type OperationAction } from '@/api/schema-rules';
import type { DerivationVariant, SourceType } from '@/api/types';
import type { ElementDocument, SchemaDocument } from './document';
import { asList, isRecord, moveItem, removeKey, renameKey, type ElementIds } from './form-helpers';

type Doc = Record<string, unknown>;

/** Set `key`, or remove it when `value` is undefined, empty text or an empty list. */
export function setOrDelete(target: Doc, key: string, value: unknown) {
  const empty = value === undefined || value === '' || (Array.isArray(value) && value.length === 0);
  if (empty) delete target[key];
  else target[key] = value;
}

/** A whole number typed in a field, or undefined when the field is empty or not a number. */
export function wholeNumber(text: string): number | undefined {
  if (text.trim() === '') return undefined;
  const value = Number(text);
  return Number.isInteger(value) ? value : undefined;
}

/** The mapping under `key`, created when missing or not a mapping. */
export function child(target: Doc, key: string): Doc {
  const value = target[key];
  if (isRecord(value)) return value;
  const created: Doc = {};
  target[key] = created;
  return created;
}

const kindNames = (draft: SchemaDocument) => Object.keys(draft.kinds ?? {});
const relationNames = (draft: SchemaDocument) => Object.keys(draft.relations ?? {});

// ------------------------------------------------------------------ Operations

export function addOperation(draft: SchemaDocument, name: string, ids: ElementIds) {
  const operation: ElementDocument = {
    id: ids('opr', `operations/${name}`),
    parameters: [],
    steps: [{ step: 'create_record', kind: kindNames(draft)[0] ?? '', values: {} }],
    review_policy: { mode: 'never' },
  };
  draft.operations = { ...draft.operations, [name]: operation };
}

export function renameOperation(draft: SchemaDocument, from: string, to: string) {
  if (draft.operations === undefined || from === to) return;
  draft.operations = renameKey(draft.operations, from, to);
}

const STEP_KEYS = ['kind', 'relation', 'target', 'from', 'to', 'values'];

/** Switch a step to `action`: fields the action does not take go, missing ones are added. */
export function changeStepAction(step: Doc, action: OperationAction, draft: SchemaDocument) {
  step['step'] = action;
  const fields = STEP_FIELDS[action];
  for (const key of STEP_KEYS) if (!fields.includes(key)) delete step[key];
  if (fields.includes('kind') && typeof step['kind'] !== 'string') {
    step['kind'] = kindNames(draft)[0] ?? '';
  }
  if (fields.includes('relation') && typeof step['relation'] !== 'string') {
    step['relation'] = relationNames(draft)[0] ?? '';
  }
  for (const key of ['target', 'from', 'to']) {
    if (fields.includes(key) && typeof step[key] !== 'string') step[key] = '';
  }
}

const STEP_REFERENCE = /\$step\[(\d+)\]/g;
const REFERENCE_FIELDS = ['target', 'from', 'to', 'values'];

/** `value` with every `$step[n]` in its text renumbered by `index`; other values as they are. */
function renumbered(value: unknown, index: number[]): unknown {
  if (typeof value === 'string') {
    return value.replace(STEP_REFERENCE, (whole, n: string) => {
      const next = index[Number(n)];
      return next === undefined ? whole : `$step[${next}]`;
    });
  }
  if (Array.isArray(value)) return value.map((v) => renumbered(v, index));
  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, renumbered(v, index)]));
  }
  return value;
}

/**
 * Move step `from` to position `to`. `$step[n]` counts steps from the top, so the references
 * of the steps are renumbered to keep pointing at the same step.
 */
export function moveStep(operation: Doc, from: number, to: number) {
  const steps = asList(operation['steps']);
  const order = moveItem(
    steps.map((_, i) => i),
    from,
    to,
  );
  if (order.every((old, i) => old === i)) return;
  const index: number[] = [];
  order.forEach((old, next) => {
    index[old] = next;
  });
  operation['steps'] = order.map((old) => {
    const step = steps[old];
    if (!isRecord(step)) return step;
    const copy: Doc = { ...step };
    for (const key of REFERENCE_FIELDS) if (key in copy) copy[key] = renumbered(copy[key], index);
    return copy;
  });
}

export type ReviewMode = 'never' | 'always' | 'expression';

/** The review policy of an Operation document: a `{mode, expression}` mapping or plain text. */
export function reviewPolicyOf(operation: Doc): { mode: ReviewMode; expression: string } {
  const policy = operation['review_policy'];
  if (typeof policy === 'string') {
    return policy === 'never' || policy === 'always'
      ? { mode: policy, expression: '' }
      : { mode: 'expression', expression: policy };
  }
  if (!isRecord(policy)) return { mode: 'never', expression: '' };
  const mode = policy['mode'];
  const expression = typeof policy['expression'] === 'string' ? policy['expression'] : '';
  return mode === 'always' || mode === 'expression'
    ? { mode, expression }
    : { mode: 'never', expression };
}

export function setReviewPolicy(operation: Doc, mode: ReviewMode, expression: string) {
  const policy = isRecord(operation['review_policy']) ? operation['review_policy'] : {};
  policy['mode'] = mode;
  if (mode === 'expression') policy['expression'] = expression;
  else delete policy['expression'];
  operation['review_policy'] = policy;
}

// ----------------------------------------------------------------- Derivations

/** Definition keys each variant takes. */
const VARIANT_FIELDS: Record<DerivationVariant, readonly string[]> = {
  expression: ['kind', 'attribute', 'expression'],
  aggregate: [
    'kind',
    'attribute',
    'relation',
    'direction',
    'function',
    'source_attribute',
    'filter',
  ],
  path: ['steps', 'inverse_api_name', 'materialized'],
};
const DEFINITION_KEYS = [...new Set(Object.values(VARIANT_FIELDS).flat())];

/** The `definition` mapping of a Derivation document, created when missing. */
export const definitionOf = (derivation: Doc): Doc => child(derivation, 'definition');

/** Point the computed Attribute of `name` at `kind.attribute`: the Attribute declares it. */
export function setDerivationTarget(
  draft: SchemaDocument,
  name: string,
  kind: string,
  attribute: string,
) {
  for (const [kindName, owner] of Object.entries(draft.kinds ?? {})) {
    for (const [attributeName, candidate] of Object.entries(owner.attributes ?? {})) {
      const isTarget = kindName === kind && attributeName === attribute;
      if (isTarget) candidate.derivation = name;
      else if (candidate.derivation === name) delete candidate.derivation;
    }
  }
  const derivation = draft.derivations?.[name];
  if (derivation === undefined) return;
  const definition = definitionOf(derivation);
  definition['kind'] = kind;
  definition['attribute'] = attribute;
}

function clearDerivationTarget(draft: SchemaDocument, name: string) {
  for (const owner of Object.values(draft.kinds ?? {})) {
    for (const attribute of Object.values(owner.attributes ?? {})) {
      if (attribute.derivation === name) delete attribute.derivation;
    }
  }
}

/** Switch a Derivation to `variant`, keeping the keys both variants share and unknown ones. */
export function changeVariant(draft: SchemaDocument, name: string, variant: DerivationVariant) {
  const derivation = draft.derivations?.[name];
  if (derivation === undefined) return;
  const definition = definitionOf(derivation);
  definition['variant'] = variant;
  const fields = VARIANT_FIELDS[variant];
  for (const key of DEFINITION_KEYS) if (!fields.includes(key)) delete definition[key];
  const relations = relationNames(draft);
  if (variant === 'path') {
    clearDerivationTarget(draft, name);
    if (asList(definition['steps']).length === 0) {
      definition['steps'] = relations.slice(0, 2).map((relation) => ({ relation }));
    }
    return;
  }
  if (typeof definition['kind'] !== 'string') definition['kind'] = kindNames(draft)[0] ?? '';
  if (typeof definition['attribute'] !== 'string') definition['attribute'] = '';
  if (variant === 'expression' && typeof definition['expression'] !== 'string') {
    definition['expression'] = '';
  }
  if (variant === 'aggregate') {
    if (typeof definition['relation'] !== 'string') definition['relation'] = relations[0] ?? '';
    if (typeof definition['function'] !== 'string') definition['function'] = 'count';
  }
  const attribute = definition['attribute'];
  if (attribute !== '')
    setDerivationTarget(draft, name, String(definition['kind']), String(attribute));
}

export function addDerivation(draft: SchemaDocument, name: string, ids: ElementIds) {
  const derivation: ElementDocument = {
    id: ids('drv', `derivations/${name}`),
    definition: {
      variant: 'expression',
      kind: kindNames(draft)[0] ?? '',
      attribute: '',
      expression: '',
    },
  };
  draft.derivations = { ...draft.derivations, [name]: derivation };
}

export function renameDerivation(draft: SchemaDocument, from: string, to: string) {
  if (draft.derivations === undefined || from === to) return;
  draft.derivations = renameKey(draft.derivations, from, to);
  for (const owner of Object.values(draft.kinds ?? {})) {
    for (const attribute of Object.values(owner.attributes ?? {})) {
      if (attribute.derivation === from) attribute.derivation = to;
    }
  }
}

export function removeDerivation(draft: SchemaDocument, name: string) {
  draft.derivations = removeKey(draft.derivations, name);
  clearDerivationTarget(draft, name);
}

// --------------------------------------------------------------------- Sources

/** Source fields shown for some types only; `table` and `query` belong to Postgres. */
const TYPE_ONLY_FIELDS: Record<string, readonly string[]> = {
  table: ['postgres_table'],
  query: ['postgres_query'],
  ...SOURCE_TYPE_FIELDS,
};

export const appliesTo = (field: string, type: string) =>
  TYPE_ONLY_FIELDS[field]?.includes(type) ?? true;

/** Switch a Source to `type`; fields the new type does not take are removed. */
export function changeSourceType(source: Doc, type: SourceType) {
  source['type'] = type;
  for (const field of Object.keys(TYPE_ONLY_FIELDS)) {
    if (!appliesTo(field, type)) delete source[field];
  }
}

export function addSource(draft: SchemaDocument, name: string, ids: ElementIds) {
  const kind = kindNames(draft)[0] ?? '';
  const source: ElementDocument = {
    id: ids('src', `sources/${name}`),
    type: 'csv',
    connection: `secret://env/${name.replace(/\./g, '_').toUpperCase()}`,
    target_kind: kind,
    key_mapping: [...(draft.kinds?.[kind]?.primary_key ?? [])],
    attribute_mapping: {},
    sync_mode: 'snapshot',
  };
  draft.sources = { ...draft.sources, [name]: source };
}

export function renameSource(draft: SchemaDocument, from: string, to: string) {
  if (draft.sources === undefined || from === to) return;
  draft.sources = renameKey(draft.sources, from, to);
  for (const owner of Object.values(draft.kinds ?? {})) {
    for (const attribute of Object.values(owner.attributes ?? {})) {
      if (attribute.authority === from) attribute.authority = to;
    }
  }
}

export function removeSource(draft: SchemaDocument, name: string) {
  draft.sources = removeKey(draft.sources, name);
  for (const owner of Object.values(draft.kinds ?? {})) {
    for (const attribute of Object.values(owner.attributes ?? {})) {
      if (attribute.authority === name) delete attribute.authority;
    }
  }
}
