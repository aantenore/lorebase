/**
 * The schema as a document (packages/core/src/lorebase_core/model/yaml_schema.py): elements
 * keyed by api name, references by api name, stable ids optional on input and always
 * written on output. `schemaToDocument` and `documentToSchema` convert between that layout
 * and the id-keyed `Schema` the Keeper API serves, so the Modeler edits one document in
 * two views.
 */
import { DEFAULT_DISPLAY } from '@/api/display-names';
import type { SchemaBody, SchemaIssue } from '@/api/schema-rules';
import type {
  Attribute,
  AttributeConstraints,
  AttributeType,
  Derivation,
  Kind,
  Lens,
  Operation,
  OperationParameter,
  Relation,
  Schema,
  SearchWeight,
  Source,
  Trait,
} from '@/api/types';
import { parseAttributeType } from '@/api/values';
import { modeler as modelerMessages } from '@/messages/modeler';

export const SCHEMA_FORMAT = 'lorebase.schema/v1';

/** A type as the document spells it: `string`, `decimal(12,2)`, `ref(Kind)`, `{enum: [...]}`, `{array: ...}`. */
export type TypeDocument = string | { enum: string[] } | { array: string | { enum: string[] } };

export interface AttributeDocument {
  id?: string;
  display_name?: string;
  description?: string;
  type: TypeDocument;
  required?: boolean;
  unique?: boolean;
  default?: unknown;
  constraints?: AttributeConstraints;
  sensitivity?: string[];
  indexed?: boolean;
  searchable?: boolean;
  search_weight?: SearchWeight;
  /** Api name of the Derivation that computes the value. */
  derivation?: string;
  /** Api name of the Source that owns the value. */
  authority?: string;
  vault?: boolean;
  valid_time?: boolean;
}

export interface TraitImplementationDocument {
  /** Api name of the Trait. */
  trait: string;
  /** Trait Attribute api name to Kind Attribute api name. */
  attribute_map: Record<string, string>;
}

export interface KindDocument {
  id?: string;
  display_name?: string;
  description?: string;
  /** Attribute api names. */
  primary_key?: string[];
  title_attribute?: string;
  icon?: string;
  color?: string;
  status?: string;
  /** Role names that steward the Kind. */
  stewards?: string[];
  /** Semantic search: `embedding_template` opts the Kind in (placeholders are Attribute names). */
  search?: { embedding_template?: string; embedding_sensitive?: boolean };
  operations_builtin?: { create?: boolean; update?: boolean; delete?: boolean };
  attributes?: Record<string, AttributeDocument>;
  traits?: TraitImplementationDocument[];
}

export interface RelationDocument {
  id?: string;
  display_name?: string;
  description?: string;
  inverse_api_name: string;
  from_kind: string;
  to_kind: string;
  cardinality?: string;
  required_from?: boolean;
  on_delete?: string;
  backing?: string;
  /** Api name of the `from` Kind Attribute that holds the target key. */
  foreign_key_attribute?: string;
  attributes?: Record<string, AttributeDocument>;
}

export interface TraitDocument {
  id?: string;
  display_name?: string;
  description?: string;
  attributes?: Record<
    string,
    { id?: string; type: TypeDocument; required?: boolean; description?: string }
  >;
}

/**
 * Operations, Derivations and Sources stay loose mappings: their forms edit the keys they
 * know in place (`element-edits.ts`), so any other key survives a form edit.
 */
export type ElementDocument = Record<string, unknown>;

export interface SchemaDocument {
  format?: string;
  domain?: { id?: string } & Record<string, unknown>;
  kinds?: Record<string, KindDocument>;
  relations?: Record<string, RelationDocument>;
  traits?: Record<string, TraitDocument>;
  operations?: Record<string, ElementDocument>;
  derivations?: Record<string, ElementDocument>;
  sources?: Record<string, ElementDocument>;
  lenses?: Record<string, ElementDocument>;
}

export type IdFactory = (prefix: string, key: string) => string;

const PREFIX = {
  kind: 'knd',
  attribute: 'att',
  relation: 'rel',
  trait: 'trt',
  operation: 'opr',
  derivation: 'drv',
  source: 'src',
  lens: 'lns',
} as const;

type Doc = Record<string, unknown>;

const isMap = (value: unknown): value is Doc =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const nonEmpty = (value: Doc): Doc | undefined =>
  Object.keys(value).length > 0 ? value : undefined;

function put(target: Doc, key: string, value: unknown) {
  if (value !== undefined) target[key] = value;
}

/** Writes the members the view does not model back as they were; a modeled key is never replaced. */
function withExtra(out: Doc, extra: Record<string, unknown> | undefined): Doc {
  for (const [key, value] of Object.entries(extra ?? {})) {
    if (!(key in out)) out[key] = structuredClone(value);
  }
  return out;
}

/** The members of a document mapping outside `known`, as the `extra` the view carries. */
const extraOfDocument = (doc: Doc, known: readonly string[]): { extra?: Doc } => {
  const rest = Object.fromEntries(Object.entries(doc).filter(([key]) => !known.includes(key)));
  return Object.keys(rest).length === 0 ? {} : { extra: structuredClone(rest) };
};

// Members of each document mapping that the readers below consume; any other is carried.
const ATTRIBUTE_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'type',
  'required',
  'unique',
  'default',
  'constraints',
  'sensitivity',
  'indexed',
  'searchable',
  'search_weight',
  'derivation',
  'authority',
  'vault',
  'valid_time',
] as const;
const KIND_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'primary_key',
  'title_attribute',
  'attributes',
  'traits',
  'status',
  'search',
  'icon',
  'color',
  'operations_builtin',
  'stewards',
] as const;
const RELATION_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'inverse_api_name',
  'from_kind',
  'to_kind',
  'cardinality',
  'required_from',
  'attributes',
  'on_delete',
  'backing',
  'foreign_key_attribute',
  'status',
] as const;
const TRAIT_DOCUMENT_KEYS = ['id', 'display_name', 'description', 'attributes', 'status'] as const;
const TRAIT_ATTRIBUTE_DOCUMENT_KEYS = [
  'id',
  'type',
  'required',
  'display_name',
  'description',
] as const;
const OPERATION_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'parameters',
  'preconditions',
  'steps',
  'review_policy',
  'permissions',
  'side_effects',
  'rate_limit',
  'status',
] as const;
const DERIVATION_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'definition',
  'status',
] as const;
const LENS_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'kinds',
  'traits',
  'relations',
] as const;
const SCHEMA_DOCUMENT_SECTIONS = [
  'format',
  'domain',
  'kinds',
  'relations',
  'traits',
  'operations',
  'derivations',
  'sources',
  'lenses',
] as const;

// ------------------------------------------------------------- model to document

function typeToDocument(
  type: AttributeType | 'ref',
  constraints: AttributeConstraints,
): TypeDocument {
  const values = constraints.enum_values ?? [];
  if (type === 'enum') return { enum: values };
  if (type === 'array<enum>') return { array: { enum: values } };
  const array = /^array<(.+)>$/.exec(type);
  return array?.[1] === undefined ? type : { array: array[1] };
}

interface NameLookup {
  derivation: (id: string) => string | undefined;
  source: (id: string) => string | undefined;
}

function attributeToDocument(attribute: Attribute, lookup: NameLookup): AttributeDocument {
  const constraints: AttributeConstraints = { ...attribute.constraints };
  delete constraints.enum_values;
  const out: Doc = { id: attribute.id };
  if (attribute.display_name !== DEFAULT_DISPLAY.attribute(attribute.api_name)) {
    out['display_name'] = attribute.display_name;
  }
  if (attribute.description !== '') out['description'] = attribute.description;
  out['type'] = typeToDocument(attribute.type, attribute.constraints);
  if (attribute.required) out['required'] = true;
  if (attribute.unique) out['unique'] = true;
  if (attribute.default !== null && attribute.default !== undefined) {
    out['default'] = attribute.default;
  }
  put(out, 'constraints', nonEmpty({ ...constraints }));
  if (attribute.sensitivity.length > 0) out['sensitivity'] = [...attribute.sensitivity];
  if (attribute.indexed) out['indexed'] = true;
  if (attribute.searchable !== null) {
    out['searchable'] = true;
    out['search_weight'] = attribute.searchable;
  }
  if (attribute.derivation_id !== null) {
    out['derivation'] = lookup.derivation(attribute.derivation_id) ?? attribute.derivation_id;
  }
  if (attribute.authority !== null) {
    out['authority'] = lookup.source(attribute.authority) ?? attribute.authority;
  }
  if (attribute.vault === true) out['vault'] = true;
  if (attribute.valid_time === true) out['valid_time'] = true;
  return withExtra(out, attribute.extra) as unknown as AttributeDocument;
}

function attributesToDocument(
  attributes: Record<string, Attribute>,
  lookup: NameLookup,
): Record<string, AttributeDocument> {
  return Object.fromEntries(
    Object.values(attributes).map((a) => [a.api_name, attributeToDocument(a, lookup)]),
  );
}

function parameterToDocument(parameter: OperationParameter): Doc {
  let type: TypeDocument = parameter.type;
  if (parameter.type === 'ref' && parameter.kind_ref !== undefined) {
    type = `ref(${parameter.kind_ref})`;
  } else if (parameter.type === 'enum') {
    type = { enum: parameter.enum_values ?? [] };
  }
  const out: Doc = { name: parameter.name, type };
  if (parameter.required) out['required'] = true;
  if (parameter.description !== '') out['description'] = parameter.description;
  return out;
}

const RATE_LIMIT = /^(\d+) per (\d+)s$/;

function operationToDocument(operation: Operation): ElementDocument {
  const out: Doc = { id: operation.id };
  if (operation.display_name !== operation.api_name) out['display_name'] = operation.display_name;
  if (operation.description !== '') out['description'] = operation.description;
  out['parameters'] = operation.parameters.map(parameterToDocument);
  if (operation.preconditions.length > 0) out['preconditions'] = operation.preconditions;
  out['steps'] = operation.steps.map((s) => ({ step: s.action, ...s.with }));
  out['review_policy'] =
    operation.review_policy === 'never' || operation.review_policy === 'always'
      ? { mode: operation.review_policy }
      : { mode: 'expression', expression: operation.review_policy };
  if (operation.permissions.length > 0) out['permissions'] = operation.permissions;
  if (operation.side_effects.length > 0) out['side_effects'] = operation.side_effects;
  const limit = operation.rate_limit === null ? null : RATE_LIMIT.exec(operation.rate_limit);
  if (limit?.[1] !== undefined && limit[2] !== undefined) {
    out['rate_limit'] = { max_calls: Number(limit[1]), per_seconds: Number(limit[2]) };
  } else if (operation.rate_limit !== null) {
    out['rate_limit'] = operation.rate_limit;
  }
  if (operation.status !== 'active') out['status'] = operation.status;
  return withExtra(out, operation.extra);
}

function derivationToDocument(
  derivation: Derivation,
  kindName: (id: string) => string,
): ElementDocument {
  const out: Doc = { id: derivation.id };
  if (derivation.display_name !== DEFAULT_DISPLAY.derivation(derivation.api_name)) {
    out['display_name'] = derivation.display_name;
  }
  if (derivation.description !== '') out['description'] = derivation.description;
  if (derivation.status !== undefined) out['status'] = derivation.status;
  out['definition'] =
    derivation.variant === 'path'
      ? { variant: derivation.variant, ...derivation.definition }
      : {
          variant: derivation.variant,
          kind: kindName(derivation.target.kind_id),
          attribute: derivation.target.api_name,
          ...derivation.definition,
        };
  return withExtra(out, derivation.extra);
}

function sourceToDocument(
  source: Source,
  kindName: (id: string) => string,
  attributeName: (id: string) => string,
  relationName: (id: string) => string,
): ElementDocument {
  const out: Doc = { id: source.id };
  if (source.display_name !== undefined) out['display_name'] = source.display_name;
  if (source.description !== undefined) out['description'] = source.description;
  if (source.status !== undefined) out['status'] = source.status;
  out['type'] = source.type;
  out['connection'] = source.connection;
  put(out, 'table', source.table);
  put(out, 'query', source.query);
  put(out, 'records_path', source.records_path);
  put(out, 'flatten_separator', source.flatten_separator);
  put(out, 'http', source.http);
  out['target_kind'] = kindName(source.target_kind);
  out['key_mapping'] = source.key_mapping;
  out['attribute_mapping'] = Object.fromEntries(
    Object.entries(source.attribute_mapping).map(([id, mapping]) => [attributeName(id), mapping]),
  );
  if (source.relation_mapping.length > 0) {
    out['relation_mapping'] = source.relation_mapping.map((m) => ({
      relation: relationName(m.relation),
      columns: m.columns,
    }));
  }
  if (source.scope_filter !== null) out['scope_filter'] = source.scope_filter;
  out['sync_mode'] = source.sync_mode;
  if (source.watermark_column != null) out['watermark_column'] = source.watermark_column;
  if (source.tombstone_column != null) out['tombstone_column'] = source.tombstone_column;
  if (source.schedule !== null) out['schedule'] = source.schedule;
  if (Object.keys(source.target_branch_policy).length > 0) {
    out['target_branch_policy'] = source.target_branch_policy;
  }
  if (source.local_edits !== undefined) out['local_edits'] = source.local_edits;
  if (source.stale_after !== undefined) out['stale_after'] = source.stale_after;
  for (const [key, value] of Object.entries(source.extra ?? {})) {
    if (!(key in out)) out[key] = value;
  }
  return out;
}

/** The document form of a schema. Built-in Operations are generated by Keeper and left out. */
export function schemaToDocument(
  schema: SchemaBody & Pick<Schema, 'domain' | 'domain_info' | 'lenses' | 'extra'>,
): SchemaDocument {
  const names = new Map<string, string>();
  for (const kind of schema.kinds) {
    names.set(kind.id, kind.api_name);
    for (const a of Object.values(kind.attributes)) names.set(a.id, a.api_name);
  }
  for (const element of [...schema.relations, ...schema.derivations, ...schema.sources]) {
    names.set(element.id, element.api_name);
  }
  const name = (id: string) => names.get(id) ?? id;
  const lookup: NameLookup = {
    derivation: (id) => schema.derivations.find((d) => d.id === id)?.api_name,
    source: (id) => schema.sources.find((s) => s.id === id)?.api_name,
  };

  const { extra: domainExtra, ...domainInfo } = schema.domain_info ?? {};
  const doc: SchemaDocument = {
    format: SCHEMA_FORMAT,
    domain: withExtra({ id: schema.domain, ...domainInfo }, domainExtra),
  };

  doc.kinds = Object.fromEntries(
    schema.kinds.map((kind) => {
      const out: Doc = { id: kind.id };
      if (kind.display_name !== kind.api_name) out['display_name'] = kind.display_name;
      if (kind.description !== '') out['description'] = kind.description;
      if (kind.primary_key.length > 0) out['primary_key'] = kind.primary_key.map(name);
      out['title_attribute'] = name(kind.title_attribute);
      if (kind.icon !== null) out['icon'] = kind.icon;
      if (kind.color !== null) out['color'] = kind.color;
      if (kind.status !== 'active') out['status'] = kind.status;
      const search: Doc = {};
      if (kind.search.embedding_template !== null) {
        search['embedding_template'] = kind.search.embedding_template;
      }
      if (kind.search.embedding_sensitive !== null) {
        search['embedding_sensitive'] = kind.search.embedding_sensitive;
      }
      if (Object.keys(search).length > 0) out['search'] = search;
      const builtin = kind.operations_builtin;
      if (!(builtin.create && builtin.update && builtin.delete)) {
        out['operations_builtin'] = { ...builtin };
      }
      out['attributes'] = attributesToDocument(kind.attributes, lookup);
      if (kind.stewards !== undefined && kind.stewards.length > 0) out['stewards'] = kind.stewards;
      if (kind.traits.length > 0) {
        out['traits'] = kind.traits.map((t) => {
          const trait = schema.traits.find((x) => x.id === t.trait_id);
          return {
            trait: trait?.api_name ?? t.trait_id,
            attribute_map: Object.fromEntries(
              Object.entries(t.attribute_map).map(([from, to]) => [
                trait?.attributes.find((a) => a.id === from)?.name ?? from,
                name(to),
              ]),
            ),
          };
        });
      }
      return [kind.api_name, withExtra(out, kind.extra)];
    }),
  );

  doc.relations = Object.fromEntries(
    schema.relations.map((relation) => {
      const out: Doc = { id: relation.id };
      if (relation.display_name !== DEFAULT_DISPLAY.relation(relation.api_name)) {
        out['display_name'] = relation.display_name;
      }
      if (relation.description !== '') out['description'] = relation.description;
      out['inverse_api_name'] = relation.inverse_api_name;
      out['from_kind'] = name(relation.from_kind);
      out['to_kind'] = name(relation.to_kind);
      out['cardinality'] = relation.cardinality;
      if (relation.required_from) out['required_from'] = true;
      if (relation.on_delete !== 'cascade') out['on_delete'] = relation.on_delete;
      if (relation.backing !== 'explicit') out['backing'] = relation.backing;
      if (relation.status !== undefined) out['status'] = relation.status;
      if (relation.foreign_key_attribute !== undefined && relation.foreign_key_attribute !== null) {
        out['foreign_key_attribute'] = name(relation.foreign_key_attribute);
      }
      put(out, 'attributes', nonEmpty(attributesToDocument(relation.attributes, lookup)));
      withExtra(out, relation.extra);
      return [relation.api_name, out as unknown as RelationDocument];
    }),
  );

  doc.traits = Object.fromEntries(
    schema.traits.map((trait) => {
      const out: Doc = { id: trait.id };
      if (trait.display_name !== trait.api_name) out['display_name'] = trait.display_name;
      if (trait.description !== '') out['description'] = trait.description;
      if (trait.status !== undefined) out['status'] = trait.status;
      out['attributes'] = Object.fromEntries(
        trait.attributes.map((a) => [
          a.name,
          {
            id: a.id,
            ...(a.display_name === undefined ? {} : { display_name: a.display_name }),
            ...(a.description === undefined ? {} : { description: a.description }),
            type: typeToDocument(a.type, {}),
            ...(a.required ? { required: true } : {}),
            ...a.extra,
          },
        ]),
      );
      return [trait.api_name, withExtra(out, trait.extra)];
    }),
  );

  const operations = schema.operations.filter((o) => !o.builtin);
  doc.operations = Object.fromEntries(operations.map((o) => [o.api_name, operationToDocument(o)]));
  doc.derivations = Object.fromEntries(
    schema.derivations.map((d) => [d.api_name, derivationToDocument(d, name)]),
  );
  doc.sources = Object.fromEntries(
    schema.sources.map((s) => [s.api_name, sourceToDocument(s, name, name, name)]),
  );
  doc.lenses = Object.fromEntries(
    (schema.lenses ?? []).map((lens) => {
      const out: Doc = { id: lens.id };
      if (lens.display_name !== lens.api_name) out['display_name'] = lens.display_name;
      out['description'] = lens.description;
      if (lens.kinds.length > 0) out['kinds'] = lens.kinds;
      if (lens.traits.length > 0) out['traits'] = lens.traits;
      if (lens.relations !== undefined && lens.relations.length > 0) {
        out['relations'] = lens.relations;
      }
      return [lens.api_name, withExtra(out, lens.extra)];
    }),
  );
  for (const key of ['operations', 'derivations', 'sources', 'lenses'] as const) {
    if (Object.keys(doc[key] ?? {}).length === 0) delete doc[key];
  }
  for (const key of ['kinds', 'relations', 'traits'] as const) {
    if (Object.keys(doc[key] ?? {}).length === 0) delete doc[key];
  }
  // Sections the Modeler does not know are kept as they are.
  withExtra(doc as Doc, schema.extra);
  return doc;
}

// ------------------------------------------------------------- document to model

export interface Converted {
  schema: Schema;
  /** Problems found while reading the document; the schema holds what could be read. */
  issues: SchemaIssue[];
}

const ISSUE = 'invalid_document';

class Reader {
  readonly issues: SchemaIssue[] = [];

  fail(path: string, message: string, code = ISSUE) {
    this.issues.push({ path, code, message });
  }

  map(value: unknown, path: string): Doc {
    if (value === undefined || value === null) return {};
    if (isMap(value)) return value;
    this.fail(path, modelerMessages.document.mustBeMapping);
    return {};
  }

  list(value: unknown, path: string): unknown[] {
    if (value === undefined || value === null) return [];
    if (Array.isArray(value)) return value;
    this.fail(path, modelerMessages.document.mustBeList);
    return [];
  }

  string(doc: Doc, key: string, path: string, fallback: string): string {
    const value = doc[key];
    if (value === undefined || value === null) return fallback;
    if (typeof value === 'string') return value;
    this.fail(`${path}/${key}`, modelerMessages.document.mustBeText);
    return fallback;
  }

  optionalString(doc: Doc, key: string, path: string): string | null {
    const value = doc[key];
    if (value === undefined || value === null) return null;
    if (typeof value === 'string') return value;
    this.fail(`${path}/${key}`, modelerMessages.document.mustBeText);
    return null;
  }

  bool(doc: Doc, key: string, path: string, fallback: boolean): boolean {
    const value = doc[key];
    if (value === undefined || value === null) return fallback;
    if (typeof value === 'boolean') return value;
    this.fail(`${path}/${key}`, modelerMessages.document.mustBeBoolean);
    return fallback;
  }

  optionalBool(doc: Doc, key: string, path: string): boolean | null {
    const value = doc[key];
    if (value === undefined || value === null) return null;
    if (typeof value === 'boolean') return value;
    this.fail(`${path}/${key}`, modelerMessages.document.mustBeBoolean);
    return null;
  }

  strings(value: unknown, path: string): string[] {
    return this.list(value, path).filter((item, index): item is string => {
      if (typeof item === 'string') return true;
      this.fail(`${path}/${index}`, modelerMessages.document.mustBeText);
      return false;
    });
  }
}

interface IdMaps {
  kinds: Map<string, string>;
  relations: Map<string, string>;
  traits: Map<string, string>;
  operations: Map<string, string>;
  derivations: Map<string, string>;
  sources: Map<string, string>;
}

/** `status` of an element that is not active; an active one leaves it out. */
function statusOf(
  doc: Doc,
  path: string,
  reader: Reader,
): { status?: 'draft' | 'active' | 'deprecated' } {
  const status = reader.string(doc, 'status', path, 'active');
  return status === 'active' ? {} : { status: status as 'draft' | 'deprecated' };
}

function explicitId(doc: unknown): string | null {
  return isMap(doc) && typeof doc['id'] === 'string' && doc['id'] !== '' ? doc['id'] : null;
}

function collectIds<T extends { id: string; api_name: string }>(
  section: Doc,
  base: T[],
  prefix: string,
  scope: string,
  mint: IdFactory,
): Map<string, string> {
  const ids = new Map<string, string>();
  for (const [name, doc] of Object.entries(section)) {
    const known = base.find((e) => e.api_name === name)?.id;
    ids.set(name, explicitId(doc) ?? known ?? mint(prefix, `${scope}/${name}`));
  }
  return ids;
}

interface Typed {
  type: AttributeType;
  enumValues: string[] | null;
}

function typeFromDocument(value: unknown, reader: Reader, path: string): Typed {
  const fail = (): Typed => {
    reader.fail(path, modelerMessages.document.mustBeType);
    return { type: 'string', enumValues: null };
  };
  if (typeof value === 'string') {
    return parseAttributeType(value) === null && value !== ''
      ? (reader.fail(path, modelerMessages.document.unknownType(value), 'unknown_type'),
        { type: 'string', enumValues: null })
      : { type: value as AttributeType, enumValues: null };
  }
  if (!isMap(value)) return fail();
  const values = (candidate: unknown): string[] | null =>
    Array.isArray(candidate) && candidate.every((v) => typeof v === 'string') ? candidate : null;
  if ('enum' in value) {
    const listed = values(value['enum']);
    return listed === null ? fail() : { type: 'enum', enumValues: listed };
  }
  if ('array' in value) {
    const inner = value['array'];
    if (typeof inner === 'string' && parseAttributeType(`array<${inner}>`) !== null) {
      return { type: `array<${inner as 'string'}>`, enumValues: null };
    }
    if (isMap(inner) && 'enum' in inner) {
      const listed = values(inner['enum']);
      return listed === null ? fail() : { type: 'array<enum>', enumValues: listed };
    }
  }
  return fail();
}

interface ReadContext {
  reader: Reader;
  mint: IdFactory;
  ids: IdMaps;
}

function attributeFromDocument(
  name: string,
  value: unknown,
  path: string,
  scope: string,
  baseOwner: { attributes: Record<string, Attribute> } | undefined,
  ctx: ReadContext,
): Attribute | null {
  const { reader } = ctx;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const known = Object.values(baseOwner?.attributes ?? {}).find((a) => a.api_name === name)?.id;
  const typed = typeFromDocument(value['type'], reader, `${path}/type`);
  const constraints: AttributeConstraints = {
    ...reader.map(value['constraints'], `${path}/constraints`),
  };
  if (typed.enumValues !== null) constraints.enum_values = typed.enumValues;
  const weight = reader.string(value, 'search_weight', path, 'D') as SearchWeight;
  const searchable = reader.bool(value, 'searchable', path, false);
  const derivation = reader.optionalString(value, 'derivation', path);
  const authority = reader.optionalString(value, 'authority', path);
  return {
    id: explicitId(value) ?? known ?? ctx.mint(PREFIX.attribute, `${scope}/${name}`),
    api_name: name,
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.attribute(name)),
    description: reader.string(value, 'description', path, ''),
    type: typed.type,
    required: reader.bool(value, 'required', path, false),
    unique: reader.bool(value, 'unique', path, false),
    default: value['default'] ?? null,
    constraints,
    sensitivity: reader.strings(
      value['sensitivity'],
      `${path}/sensitivity`,
    ) as Attribute['sensitivity'],
    indexed: reader.bool(value, 'indexed', path, false),
    searchable: searchable ? weight : null,
    derivation_id: derivation === null ? null : (ctx.ids.derivations.get(derivation) ?? null),
    authority: authority === null ? null : (ctx.ids.sources.get(authority) ?? authority),
    ...(reader.bool(value, 'vault', path, false) ? { vault: true } : {}),
    ...(reader.bool(value, 'valid_time', path, false) ? { valid_time: true } : {}),
    ...extraOfDocument(value, ATTRIBUTE_DOCUMENT_KEYS),
  };
}

function attributesFromDocument(
  section: unknown,
  path: string,
  scope: string,
  baseOwner: { attributes: Record<string, Attribute> } | undefined,
  ctx: ReadContext,
): Record<string, Attribute> {
  const out: Record<string, Attribute> = {};
  for (const [name, value] of Object.entries(ctx.reader.map(section, path))) {
    const attribute = attributeFromDocument(name, value, `${path}/${name}`, scope, baseOwner, ctx);
    if (attribute !== null) out[attribute.id] = attribute;
  }
  return out;
}

function ref(
  ids: Map<string, string>,
  name: string,
  path: string,
  what: string,
  reader: Reader,
): string {
  const id = ids.get(name);
  if (id !== undefined) return id;
  reader.fail(path, modelerMessages.document.unknownReference(what, name), 'unknown_reference');
  return '';
}

function kindFromDocument(
  name: string,
  value: unknown,
  base: Schema,
  traits: Trait[],
  ctx: ReadContext,
): Kind | null {
  const { reader } = ctx;
  const path = `kinds/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const id = ctx.ids.kinds.get(name) ?? '';
  const baseKind = base.kinds.find((k) => k.id === id);
  const attributes = attributesFromDocument(
    value['attributes'],
    `${path}/attributes`,
    path,
    baseKind,
    ctx,
  );
  const byName = new Map(Object.values(attributes).map((a) => [a.api_name, a.id]));
  const attributeRef = (attribute: string, at: string) => {
    const found = byName.get(attribute);
    if (found === undefined) {
      reader.fail(at, modelerMessages.document.unknownAttribute(attribute), 'unknown_attribute');
      return '';
    }
    return found;
  };
  const primaryKey = reader
    .strings(value['primary_key'], `${path}/primary_key`)
    .map((n) => attributeRef(n, `${path}/primary_key`));
  const titleName = reader.optionalString(value, 'title_attribute', path);
  const first = primaryKey[0] ?? Object.keys(attributes)[0] ?? '';
  const title = titleName === null ? first : attributeRef(titleName, `${path}/title_attribute`);

  const implementations = reader.list(value['traits'], `${path}/traits`).flatMap((raw, index) => {
    const at = `${path}/traits/${index}`;
    if (!isMap(raw)) {
      reader.fail(at, modelerMessages.document.mustBeMapping);
      return [];
    }
    const traitName = reader.string(raw, 'trait', at, '');
    const trait = traits.find((t) => t.api_name === traitName);
    if (trait === undefined) {
      reader.fail(
        `${at}/trait`,
        modelerMessages.document.unknownTrait(traitName),
        'unknown_reference',
      );
      return [];
    }
    const map: Record<string, string> = {};
    for (const [from, to] of Object.entries(
      reader.map(raw['attribute_map'], `${at}/attribute_map`),
    )) {
      const traitAttribute = trait.attributes.find((a) => a.name === from);
      if (traitAttribute === undefined) {
        reader.fail(
          `${at}/attribute_map`,
          modelerMessages.document.traitWithoutAttribute(traitName, from),
          'unknown_attribute',
        );
      } else if (typeof to === 'string') {
        map[traitAttribute.id] = attributeRef(to, `${at}/attribute_map`);
      }
    }
    return [{ trait_id: trait.id, attribute_map: map }];
  });

  const builtinDoc = reader.map(value['operations_builtin'], `${path}/operations_builtin`);
  const searchDoc = reader.map(value['search'], `${path}/search`);
  const stewards = reader.strings(value['stewards'], `${path}/stewards`);
  return {
    id,
    api_name: name,
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.kind(name)),
    description: reader.string(value, 'description', path, ''),
    primary_key: primaryKey,
    title_attribute: title,
    attributes,
    traits: implementations,
    status: reader.string(value, 'status', path, 'active') as Kind['status'],
    search: {
      text_attributes: Object.values(attributes)
        .filter((a) => a.searchable !== null)
        .map((a) => ({ attribute_id: a.id, weight: a.searchable ?? 'D' })),
      embedding_template: reader.optionalString(searchDoc, 'embedding_template', `${path}/search`),
      embedding_sensitive: reader.optionalBool(searchDoc, 'embedding_sensitive', `${path}/search`),
    },
    icon: reader.optionalString(value, 'icon', path),
    color: reader.optionalString(value, 'color', path),
    operations_builtin: {
      create: reader.bool(builtinDoc, 'create', `${path}/operations_builtin`, true),
      update: reader.bool(builtinDoc, 'update', `${path}/operations_builtin`, true),
      delete: reader.bool(builtinDoc, 'delete', `${path}/operations_builtin`, true),
    },
    ...(stewards.length === 0 ? {} : { stewards }),
    ...extraOfDocument(value, KIND_DOCUMENT_KEYS),
  };
}

function relationFromDocument(
  name: string,
  value: unknown,
  base: Schema,
  kinds: Kind[],
  ctx: ReadContext,
): Relation | null {
  const { reader } = ctx;
  const path = `relations/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const id = ctx.ids.relations.get(name) ?? '';
  const baseRelation = base.relations.find((r) => r.id === id);
  const fromName = reader.string(value, 'from_kind', path, '');
  const toName = reader.string(value, 'to_kind', path, '');
  const from = ref(ctx.ids.kinds, fromName, `${path}/from_kind`, 'Kind', reader);
  const to = ref(ctx.ids.kinds, toName, `${path}/to_kind`, 'Kind', reader);
  const fkName = reader.optionalString(value, 'foreign_key_attribute', path);
  const fromKind = kinds.find((k) => k.id === from);
  const fk =
    fkName === null
      ? null
      : (Object.values(fromKind?.attributes ?? {}).find((a) => a.api_name === fkName)?.id ??
        fkName);
  return {
    id,
    api_name: name,
    inverse_api_name: reader.string(value, 'inverse_api_name', path, ''),
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.relation(name)),
    description: reader.string(value, 'description', path, ''),
    from_kind: from,
    to_kind: to,
    cardinality: reader.string(
      value,
      'cardinality',
      path,
      'many_to_many',
    ) as Relation['cardinality'],
    required_from: reader.bool(value, 'required_from', path, false),
    attributes: attributesFromDocument(
      value['attributes'],
      `${path}/attributes`,
      path,
      baseRelation,
      ctx,
    ),
    on_delete: reader.string(value, 'on_delete', path, 'cascade') as Relation['on_delete'],
    backing: reader.string(value, 'backing', path, 'explicit') as Relation['backing'],
    ...(fk === null ? {} : { foreign_key_attribute: fk }),
    ...statusOf(value, path, reader),
    ...extraOfDocument(value, RELATION_DOCUMENT_KEYS),
  };
}

function traitFromDocument(
  name: string,
  value: unknown,
  base: Schema,
  ctx: ReadContext,
): Trait | null {
  const { reader } = ctx;
  const path = `traits/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const id = ctx.ids.traits.get(name) ?? '';
  const baseTrait = base.traits.find((t) => t.id === id);
  const attributes = Object.entries(reader.map(value['attributes'], `${path}/attributes`)).flatMap(
    ([attributeName, raw]) => {
      const at = `${path}/attributes/${attributeName}`;
      if (!isMap(raw)) {
        reader.fail(at, modelerMessages.document.mustBeMapping);
        return [];
      }
      const known = baseTrait?.attributes.find((a) => a.name === attributeName)?.id;
      return [
        {
          id: explicitId(raw) ?? known ?? ctx.mint(PREFIX.attribute, at),
          name: attributeName,
          type: typeFromDocument(raw['type'], reader, `${at}/type`).type,
          required: reader.bool(raw, 'required', at, false),
          ...(raw['display_name'] === undefined
            ? {}
            : { display_name: reader.string(raw, 'display_name', at, '') }),
          ...(raw['description'] === undefined
            ? {}
            : { description: reader.string(raw, 'description', at, '') }),
          ...extraOfDocument(raw, TRAIT_ATTRIBUTE_DOCUMENT_KEYS),
        },
      ];
    },
  );
  return {
    id,
    api_name: name,
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.trait(name)),
    description: reader.string(value, 'description', path, ''),
    attributes,
    ...statusOf(value, path, reader),
    ...extraOfDocument(value, TRAIT_DOCUMENT_KEYS),
  };
}

function parameterFromDocument(
  raw: unknown,
  path: string,
  reader: Reader,
): OperationParameter | null {
  if (!isMap(raw)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const type = raw['type'];
  const parameter: OperationParameter = {
    name: reader.string(raw, 'name', path, ''),
    type: 'string',
    required: reader.bool(raw, 'required', path, false),
    description: reader.string(raw, 'description', path, ''),
  };
  const refType = typeof type === 'string' ? /^ref\(([A-Za-z0-9]+)\)$/.exec(type) : null;
  if (refType?.[1] !== undefined) {
    parameter.type = 'ref';
    parameter.kind_ref = refType[1];
  } else {
    const typed = typeFromDocument(type, reader, `${path}/type`);
    parameter.type = typed.type;
    if (typed.enumValues !== null) parameter.enum_values = typed.enumValues;
  }
  return parameter;
}

function operationFromDocument(name: string, value: unknown, ctx: ReadContext): Operation | null {
  const { reader } = ctx;
  const path = `operations/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const policy = value['review_policy'];
  let reviewPolicy = 'never';
  if (typeof policy === 'string') reviewPolicy = policy;
  else if (isMap(policy)) {
    const mode = reader.string(policy, 'mode', `${path}/review_policy`, 'never');
    reviewPolicy =
      mode === 'expression'
        ? reader.string(policy, 'expression', `${path}/review_policy`, '')
        : mode;
  }
  const limit = value['rate_limit'];
  let rateLimit: string | null = null;
  if (typeof limit === 'string') rateLimit = limit;
  else if (isMap(limit))
    rateLimit = `${String(limit['max_calls'])} per ${String(limit['per_seconds'])}s`;
  return {
    id: ctx.ids.operations.get(name) ?? '',
    api_name: name,
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.operation(name)),
    description: reader.string(value, 'description', path, ''),
    parameters: reader
      .list(value['parameters'], `${path}/parameters`)
      .map((raw, i) => parameterFromDocument(raw, `${path}/parameters/${i}`, reader))
      .filter((p): p is OperationParameter => p !== null),
    preconditions: reader
      .list(value['preconditions'], `${path}/preconditions`)
      .filter(isMap)
      .map((p) => ({
        expression: reader.string(p, 'expression', `${path}/preconditions`, ''),
        message: reader.string(p, 'message', `${path}/preconditions`, ''),
      })),
    steps: reader.list(value['steps'], `${path}/steps`).flatMap((raw, index) => {
      if (!isMap(raw)) {
        reader.fail(`${path}/steps/${index}`, modelerMessages.document.mustBeMapping);
        return [];
      }
      const { step, ...rest } = raw;
      return [{ action: typeof step === 'string' ? step : '', with: rest }];
    }),
    review_policy: reviewPolicy,
    permissions: reader.strings(value['permissions'], `${path}/permissions`),
    side_effects: reader.strings(value['side_effects'], `${path}/side_effects`),
    rate_limit: rateLimit,
    status: reader.string(value, 'status', path, 'active') as Operation['status'],
    builtin: false,
    ...extraOfDocument(value, OPERATION_DOCUMENT_KEYS),
  };
}

function derivationFromDocument(name: string, value: unknown, ctx: ReadContext): Derivation | null {
  const { reader } = ctx;
  const path = `derivations/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const { variant, kind, attribute, ...rest } = reader.map(
    value['definition'],
    `${path}/definition`,
  );
  const variantName = typeof variant === 'string' ? variant : 'expression';
  const isPath = variantName === 'path';
  const kindName = typeof kind === 'string' ? kind : '';
  return {
    id: ctx.ids.derivations.get(name) ?? '',
    api_name: name,
    display_name: reader.string(value, 'display_name', path, DEFAULT_DISPLAY.derivation(name)),
    description: reader.string(value, 'description', path, ''),
    variant: variantName as Derivation['variant'],
    target: isPath
      ? { type: 'relation', kind_id: '', api_name: name }
      : {
          type: 'attribute',
          kind_id: ref(ctx.ids.kinds, kindName, `${path}/definition/kind`, 'Kind', reader),
          api_name: typeof attribute === 'string' ? attribute : '',
        },
    definition: rest,
    ...statusOf(value, path, reader),
    ...extraOfDocument(value, DERIVATION_DOCUMENT_KEYS),
  };
}

function lensFromDocument(
  name: string,
  value: unknown,
  base: Schema,
  ctx: ReadContext,
): Lens | null {
  const { reader } = ctx;
  const path = `lenses/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const relations = reader.strings(value['relations'], `${path}/relations`);
  return {
    id:
      explicitId(value) ??
      (base.lenses ?? []).find((l) => l.api_name === name)?.id ??
      ctx.mint(PREFIX.lens, path),
    api_name: name,
    display_name: reader.string(value, 'display_name', path, name),
    description: reader.string(value, 'description', path, ''),
    kinds: reader.strings(value['kinds'], `${path}/kinds`),
    traits: reader.strings(value['traits'], `${path}/traits`),
    ...(relations.length === 0 ? {} : { relations }),
    ...extraOfDocument(value, LENS_DOCUMENT_KEYS),
  };
}

/** Keys of a Source mapping that `sourceFromDocument` reads; any other key goes to `extra`. */
const SOURCE_DOCUMENT_KEYS = [
  'id',
  'display_name',
  'description',
  'status',
  'type',
  'connection',
  'table',
  'query',
  'records_path',
  'flatten_separator',
  'http',
  'target_kind',
  'key_mapping',
  'attribute_mapping',
  'relation_mapping',
  'scope_filter',
  'sync_mode',
  'watermark_column',
  'tombstone_column',
  'schedule',
  'target_branch_policy',
  'local_edits',
  'stale_after',
];

function sourceFromDocument(
  name: string,
  value: unknown,
  kinds: Kind[],
  relations: Relation[],
  ctx: ReadContext,
): Source | null {
  const { reader } = ctx;
  const path = `sources/${name}`;
  if (!isMap(value)) {
    reader.fail(path, modelerMessages.document.mustBeMapping);
    return null;
  }
  const targetName = reader.string(value, 'target_kind', path, '');
  const targetId = ref(ctx.ids.kinds, targetName, `${path}/target_kind`, 'Kind', reader);
  const target = kinds.find((k) => k.id === targetId);
  const attributeMapping: Source['attribute_mapping'] = {};
  for (const [attributeName, mapping] of Object.entries(
    reader.map(value['attribute_mapping'], `${path}/attribute_mapping`),
  )) {
    const attribute = Object.values(target?.attributes ?? {}).find(
      (a) => a.api_name === attributeName,
    );
    if (attribute === undefined) {
      reader.fail(
        `${path}/attribute_mapping/${attributeName}`,
        modelerMessages.document.unknownAttribute(attributeName),
        'unknown_attribute',
      );
    } else if (isMap(mapping)) {
      attributeMapping[attribute.id] = mapping;
    }
  }
  const relationMapping: Source['relation_mapping'] = [];
  reader.list(value['relation_mapping'], `${path}/relation_mapping`).forEach((mapping, index) => {
    const at = `${path}/relation_mapping/${index}`;
    if (!isMap(mapping)) {
      reader.fail(at, modelerMessages.document.mustBeMapping);
      return;
    }
    const relationName = reader.string(mapping, 'relation', at, '');
    const relation = relations.find((r) => r.api_name === relationName);
    if (relation === undefined) {
      reader.fail(
        `${at}/relation`,
        `Relation ${relationName} does not exist.`,
        'unknown_reference',
      );
      return;
    }
    relationMapping.push({
      relation: relation.id,
      columns: reader.strings(mapping['columns'], `${at}/columns`),
    });
  });
  const policy = reader.map(value['target_branch_policy'], `${path}/target_branch_policy`);
  const display = reader.optionalString(value, 'display_name', path);
  const described = reader.optionalString(value, 'description', path);
  const watermark = reader.optionalString(value, 'watermark_column', path);
  const tombstone = reader.optionalString(value, 'tombstone_column', path);
  const localEdits = reader.optionalString(value, 'local_edits', path);
  const staleAfter = reader.optionalString(value, 'stale_after', path);
  const branchTemplate = reader.optionalString(
    policy,
    'branch_template',
    `${path}/target_branch_policy`,
  );
  const mergePolicy = reader.optionalString(policy, 'merge_policy', `${path}/target_branch_policy`);
  const table = reader.optionalString(value, 'table', path);
  const query = reader.optionalString(value, 'query', path);
  const recordsPath = reader.optionalString(value, 'records_path', path);
  const separator = reader.optionalString(value, 'flatten_separator', path);
  const http =
    value['http'] === undefined || value['http'] === null
      ? undefined
      : reader.map(value['http'], `${path}/http`);
  const extra = Object.fromEntries(
    Object.entries(value).filter(([key]) => !SOURCE_DOCUMENT_KEYS.includes(key)),
  );
  return {
    id: ctx.ids.sources.get(name) ?? '',
    api_name: name,
    ...(display === null ? {} : { display_name: display }),
    ...(described === null ? {} : { description: described }),
    ...statusOf(value, path, reader),
    ...(watermark === null ? {} : { watermark_column: watermark }),
    ...(tombstone === null ? {} : { tombstone_column: tombstone }),
    type: reader.string(value, 'type', path, 'csv') as Source['type'],
    connection: reader.string(value, 'connection', path, ''),
    ...(table === null ? {} : { table }),
    ...(query === null ? {} : { query }),
    ...(recordsPath === null ? {} : { records_path: recordsPath }),
    ...(separator === null ? {} : { flatten_separator: separator }),
    ...(http === undefined ? {} : { http }),
    target_kind: targetId,
    key_mapping: reader.strings(value['key_mapping'], `${path}/key_mapping`),
    attribute_mapping: attributeMapping,
    relation_mapping: relationMapping,
    scope_filter: reader.optionalString(value, 'scope_filter', path),
    sync_mode: reader.string(value, 'sync_mode', path, 'snapshot') as Source['sync_mode'],
    ...(localEdits === null
      ? {}
      : { local_edits: localEdits as NonNullable<Source['local_edits']> }),
    ...(staleAfter === null ? {} : { stale_after: staleAfter }),
    schedule: reader.optionalString(value, 'schedule', path),
    target_branch_policy: {
      ...(branchTemplate === null ? {} : { branch_template: branchTemplate }),
      ...(mergePolicy === null
        ? {}
        : { merge_policy: mergePolicy as Source['target_branch_policy']['merge_policy'] }),
    },
    ...(Object.keys(extra).length === 0 ? {} : { extra }),
  };
}

const present = <T>(items: (T | null)[]): T[] => items.filter((item): item is T => item !== null);

/**
 * Read a document into a schema. Ids: an explicit `id` wins (a rename keeps the element),
 * else the element of the same api name in `base`, else `mint` makes one. Anything that
 * cannot be read is reported with its document path and left out.
 */
export function documentToSchema(document: unknown, base: Schema, mint: IdFactory): Converted {
  const reader = new Reader();
  const root = reader.map(document, '');
  const section = (key: string) => reader.map(root[key], key);
  const sections = {
    kinds: section('kinds'),
    relations: section('relations'),
    traits: section('traits'),
    operations: section('operations'),
    derivations: section('derivations'),
    sources: section('sources'),
  };
  const ids: IdMaps = {
    kinds: collectIds(sections.kinds, base.kinds, PREFIX.kind, 'kinds', mint),
    relations: collectIds(sections.relations, base.relations, PREFIX.relation, 'relations', mint),
    traits: collectIds(sections.traits, base.traits, PREFIX.trait, 'traits', mint),
    operations: collectIds(
      sections.operations,
      base.operations,
      PREFIX.operation,
      'operations',
      mint,
    ),
    derivations: collectIds(
      sections.derivations,
      base.derivations,
      PREFIX.derivation,
      'derivations',
      mint,
    ),
    sources: collectIds(sections.sources, base.sources, PREFIX.source, 'sources', mint),
  };
  const ctx: ReadContext = { reader, mint, ids };

  const traits = present(
    Object.entries(sections.traits).map(([n, v]) => traitFromDocument(n, v, base, ctx)),
  );
  const kinds = present(
    Object.entries(sections.kinds).map(([n, v]) => kindFromDocument(n, v, base, traits, ctx)),
  );
  const relations = present(
    Object.entries(sections.relations).map(([n, v]) =>
      relationFromDocument(n, v, base, kinds, ctx),
    ),
  );
  const custom = present(
    Object.entries(sections.operations).map(([n, v]) => operationFromDocument(n, v, ctx)),
  );
  const derivations = present(
    Object.entries(sections.derivations).map(([n, v]) => derivationFromDocument(n, v, ctx)),
  );
  const sources = present(
    Object.entries(sections.sources).map(([n, v]) =>
      sourceFromDocument(n, v, kinds, relations, ctx),
    ),
  );

  const lenses = present(
    Object.entries(section('lenses')).map(([n, v]) => lensFromDocument(n, v, base, ctx)),
  );
  const extra = extraOfDocument(root, SCHEMA_DOCUMENT_SECTIONS);

  const schema: Schema = {
    domain: base.domain,
    ...(base.domain_info === undefined ? {} : { domain_info: base.domain_info }),
    branch: base.branch,
    revision: base.revision,
    kinds,
    relations,
    traits,
    operations: [...base.operations.filter((o) => o.builtin), ...custom],
    derivations,
    sources,
    ...(lenses.length === 0 ? {} : { lenses }),
    ...extra,
    search: base.search,
  };
  return { schema, issues: reader.issues };
}
