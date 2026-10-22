/**
 * Keeper's JSON schema (`GET .../schema`, the `schema` member: elements keyed by id, types as
 * `{name, precision, ...}` objects, references as ids) as the view `Schema` the screens use
 * (lists of elements, types as `decimal(12,2)` strings, the layout the Modeler turns into a
 * document). Fields the view has no use for are carried when the document would lose them on
 * save (`status`, `domain_info`, and so on).
 */
import { DEFAULT_DISPLAY } from '../display-names';
import type { components } from '../generated/keeper';
import type {
  Attribute,
  AttributeConstraints,
  AttributeType,
  Derivation,
  DerivationVariant,
  ElementStatus,
  Kind,
  Operation,
  OperationParameter,
  Relation,
  Schema,
  SearchWeight,
  Source,
  Trait,
} from '../types';

type SchemaOut = components['schemas']['SchemaOut'];

type Raw = Record<string, unknown>;

interface RawType {
  name: string;
  precision?: number;
  scale?: number;
  enum_values?: string[];
  element?: RawType;
  kind_ref?: string;
}

const isRaw = (value: unknown): value is Raw =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const omit = (raw: Raw, keys: readonly string[]): Raw =>
  Object.fromEntries(Object.entries(raw).filter(([key]) => !keys.includes(key)));

const entries = (value: unknown): Raw[] => (isRaw(value) ? Object.values(value).filter(isRaw) : []);
const str = (value: unknown, fallback = ''): string =>
  typeof value === 'string' ? value : fallback;
const strOrNull = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

/** Names by id, for the spots where the document spells a reference by api name. */
class Names {
  private readonly names = new Map<string, string>();

  add(raw: Raw) {
    this.names.set(str(raw['id']), str(raw['api_name'], str(raw['name'])));
  }

  of(id: unknown): string {
    return typeof id === 'string' ? (this.names.get(id) ?? id) : '';
  }
}

/** The display name Keeper stores, or the one a document would default to when it is empty. */
const displayOf = (raw: Raw, fallback: (name: string) => string): string =>
  str(raw['display_name']) || fallback(str(raw['api_name']));

function typeName(raw: RawType, names: Names): string {
  switch (raw.name) {
    case 'decimal':
      return `decimal(${raw.precision ?? 0},${raw.scale ?? 0})`;
    case 'ref':
      return `ref(${names.of(raw.kind_ref)})`;
    case 'array':
      return `array<${raw.element === undefined ? 'string' : typeName(raw.element, names)}>`;
    default:
      return raw.name;
  }
}

/** Enum members of a type, including those of an array element. */
function enumValuesOf(raw: RawType): string[] | undefined {
  return raw.enum_values ?? raw.element?.enum_values;
}

function constraintsOf(raw: unknown, type: RawType): AttributeConstraints {
  const out: AttributeConstraints = {};
  if (isRaw(raw)) {
    for (const key of ['min', 'max', 'min_length', 'max_length', 'pattern'] as const) {
      const value = raw[key];
      if (value !== undefined && value !== null) Object.assign(out, { [key]: value });
    }
    const members = raw['enum_values'];
    if (Array.isArray(members)) out.enum_values = members.map(String);
  }
  const fromType = enumValuesOf(type);
  if (fromType !== undefined) out.enum_values = fromType;
  return out;
}

function attribute(raw: Raw, names: Names): Attribute {
  const type = (isRaw(raw['type']) ? raw['type'] : { name: 'string' }) as unknown as RawType;
  const searchable = raw['searchable'] === true;
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.attribute),
    description: str(raw['description']),
    type: typeName(type, names) as AttributeType,
    required: raw['required'] === true,
    unique: raw['unique'] === true,
    default: raw['default'] ?? null,
    constraints: constraintsOf(raw['constraints'], type),
    sensitivity: list(raw['sensitivity']).map(String) as Attribute['sensitivity'],
    indexed: raw['indexed'] === true,
    searchable: searchable ? (str(raw['search_weight'], 'D') as SearchWeight) : null,
    derivation_id: strOrNull(raw['derivation_id']),
    authority: strOrNull(raw['authority']),
  };
}

function attributes(value: unknown, names: Names): Record<string, Attribute> {
  return Object.fromEntries(
    entries(value).map((raw) => {
      const mapped = attribute(raw, names);
      return [mapped.id, mapped];
    }),
  );
}

function status(raw: Raw): { status?: ElementStatus } {
  const value = raw['status'];
  return value === undefined || value === 'active' ? {} : { status: value as ElementStatus };
}

function kind(raw: Raw, names: Names): Kind {
  const mapped = attributes(raw['attributes'], names);
  const primaryKey = list(raw['primary_key']).map(String);
  const builtin = list(raw['operations_builtin']);
  const withBuiltin =
    raw['operations_builtin'] === undefined ? ['create', 'update', 'delete'] : builtin;
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.kind),
    description: str(raw['description']),
    primary_key: primaryKey,
    title_attribute: str(raw['title_attribute'], primaryKey[0] ?? Object.keys(mapped)[0] ?? ''),
    attributes: mapped,
    traits: list(raw['traits'])
      .filter(isRaw)
      .map((t) => ({
        trait_id: str(t['trait_id']),
        attribute_map: Object.fromEntries(
          Object.entries(isRaw(t['attribute_map']) ? t['attribute_map'] : {}).map(([from, to]) => [
            from,
            String(to),
          ]),
        ),
      })),
    status: str(raw['status'], 'active') as Kind['status'],
    search: {
      text_attributes: Object.values(mapped)
        .filter((a) => a.searchable !== null)
        .map((a) => ({ attribute_id: a.id, weight: a.searchable ?? 'D' })),
      embedding_template: isRaw(raw['search'])
        ? strOrNull(raw['search']['embedding_template'])
        : null,
      embedding_sensitive:
        isRaw(raw['search']) && typeof raw['search']['embedding_sensitive'] === 'boolean'
          ? raw['search']['embedding_sensitive']
          : null,
    },
    icon: strOrNull(raw['icon']),
    color: strOrNull(raw['color']),
    operations_builtin: {
      create: withBuiltin.includes('create'),
      update: withBuiltin.includes('update'),
      delete: withBuiltin.includes('delete'),
    },
  };
}

function relation(raw: Raw, names: Names): Relation {
  const foreignKey = strOrNull(raw['foreign_key_attribute']);
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    inverse_api_name: str(raw['inverse_api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.relation),
    description: str(raw['description']),
    from_kind: str(raw['from_kind']),
    to_kind: str(raw['to_kind']),
    cardinality: str(raw['cardinality'], 'many_to_many') as Relation['cardinality'],
    required_from: raw['required_from'] === true,
    attributes: attributes(raw['attributes'], names),
    on_delete: str(raw['on_delete'], 'cascade') as Relation['on_delete'],
    backing: str(raw['backing'], 'explicit') as Relation['backing'],
    ...(foreignKey === null ? {} : { foreign_key_attribute: foreignKey }),
    ...status(raw),
  };
}

function trait(raw: Raw, names: Names): Trait {
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.trait),
    description: str(raw['description']),
    attributes: entries(raw['attributes']).map((a) => {
      const type = (isRaw(a['type']) ? a['type'] : { name: 'string' }) as unknown as RawType;
      const display = str(a['display_name']);
      const description = str(a['description']);
      return {
        id: str(a['id']),
        name: str(a['api_name']),
        type: typeName(type, names) as AttributeType,
        required: a['required'] === true,
        ...(display === '' ? {} : { display_name: display }),
        ...(description === '' ? {} : { description }),
      };
    }),
    ...status(raw),
  };
}

function parameter(raw: Raw, names: Names): OperationParameter {
  const type = (isRaw(raw['type']) ? raw['type'] : { name: 'string' }) as unknown as RawType;
  const base: OperationParameter = {
    name: str(raw['name']),
    type: 'string',
    required: raw['required'] === true,
    description: str(raw['description']),
  };
  if (type.name === 'ref') return { ...base, type: 'ref', kind_ref: names.of(type.kind_ref) };
  const enumValues = enumValuesOf(type);
  return {
    ...base,
    type: typeName(type, names) as AttributeType,
    ...(enumValues === undefined ? {} : { enum_values: enumValues }),
  };
}

/** Step members, with the references the document spells by name. */
function stepWith(raw: Raw, names: Names): Raw {
  const out: Raw = omit(raw, ['step']);
  for (const key of ['kind', 'relation'] as const) {
    if (typeof out[key] === 'string') out[key] = names.of(out[key]);
  }
  if (isRaw(out['values'])) {
    out['values'] = Object.fromEntries(
      Object.entries(out['values']).map(([id, expression]) => [names.of(id), expression]),
    );
  }
  return out;
}

function operation(raw: Raw, names: Names): Operation {
  const policy = isRaw(raw['review_policy']) ? raw['review_policy'] : {};
  const mode = str(policy['mode'], 'never');
  const limit = isRaw(raw['rate_limit']) ? raw['rate_limit'] : null;
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.operation),
    description: str(raw['description']),
    parameters: list(raw['parameters'])
      .filter(isRaw)
      .map((p) => parameter(p, names)),
    preconditions: list(raw['preconditions'])
      .filter(isRaw)
      .map((p) => ({
        expression: str(p['expression']),
        message: str(p['message']),
      })),
    steps: list(raw['steps'])
      .filter(isRaw)
      .map((s) => ({
        action: str(s['step']),
        with: stepWith(s, names),
      })),
    review_policy: mode === 'expression' ? str(policy['expression']) : mode,
    permissions: list(raw['permissions']).map(String),
    side_effects: list(raw['side_effects']).map(String),
    rate_limit:
      limit === null ? null : `${String(limit['max_calls'])} per ${String(limit['per_seconds'])}s`,
    status: str(raw['status'], 'active') as Operation['status'],
    builtin: false,
  };
}

function derivation(raw: Raw, names: Names): Derivation {
  const definition = isRaw(raw['definition']) ? raw['definition'] : {};
  const variant = str(definition['variant'], 'expression') as DerivationVariant;
  const kindId = definition['kind'];
  const attributeId = definition['attribute'];
  const body: Raw = omit(definition, ['variant', 'kind', 'attribute']);
  if (typeof body['relation'] === 'string') body['relation'] = names.of(body['relation']);
  if (typeof body['source_attribute'] === 'string') {
    body['source_attribute'] = names.of(body['source_attribute']);
  }
  if (Array.isArray(body['steps'])) {
    body['steps'] = (body['steps'] as unknown[]).map((step) =>
      isRaw(step) ? { ...step, relation: names.of(step['relation']) } : step,
    );
  }
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    display_name: displayOf(raw, DEFAULT_DISPLAY.derivation),
    description: str(raw['description']),
    variant,
    target:
      variant === 'path'
        ? { type: 'relation', kind_id: '', api_name: str(raw['api_name']) }
        : { type: 'attribute', kind_id: str(kindId), api_name: names.of(attributeId) },
    definition: body,
    ...status(raw),
  };
}

/** Keys of a raw Source that `source()` reads; any other key is carried in `extra`. */
const SOURCE_KEYS = [
  'id',
  'api_name',
  'display_name',
  'description',
  'status',
  'watermark_column',
  'tombstone_column',
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
  'schedule',
  'target_branch_policy',
] as const;

function source(raw: Raw): Source {
  const policy = isRaw(raw['target_branch_policy']) ? raw['target_branch_policy'] : {};
  const mapping = isRaw(raw['attribute_mapping']) ? raw['attribute_mapping'] : {};
  const display = str(raw['display_name']);
  const description = str(raw['description']);
  const watermark = strOrNull(raw['watermark_column']);
  const tombstone = strOrNull(raw['tombstone_column']);
  const table = strOrNull(raw['table']);
  const query = strOrNull(raw['query']);
  const recordsPath = strOrNull(raw['records_path']);
  const separator = strOrNull(raw['flatten_separator']);
  const http = isRaw(raw['http']) ? raw['http'] : undefined;
  const extra = omit(raw, SOURCE_KEYS);
  return {
    id: str(raw['id']),
    api_name: str(raw['api_name']),
    ...(display === '' ? {} : { display_name: display }),
    ...(description === '' ? {} : { description }),
    ...status(raw),
    ...(watermark === null ? {} : { watermark_column: watermark }),
    ...(tombstone === null ? {} : { tombstone_column: tombstone }),
    type: str(raw['type'], 'csv') as Source['type'],
    connection: str(raw['connection']),
    ...(table === null ? {} : { table }),
    ...(query === null ? {} : { query }),
    ...(recordsPath === null ? {} : { records_path: recordsPath }),
    ...(separator === null ? {} : { flatten_separator: separator }),
    ...(http === undefined ? {} : { http }),
    target_kind: str(raw['target_kind']),
    key_mapping: list(raw['key_mapping']).map(String),
    attribute_mapping: Object.fromEntries(
      Object.entries(mapping).filter(([, value]) => isRaw(value)),
    ) as Source['attribute_mapping'],
    relation_mapping: list(raw['relation_mapping'])
      .filter(isRaw)
      .map((m) => ({
        relation: str(m['relation']),
        columns: list(m['columns']).map(String),
      })),
    scope_filter: strOrNull(raw['scope_filter']),
    sync_mode: str(raw['sync_mode'], 'snapshot') as Source['sync_mode'],
    schedule: strOrNull(raw['schedule']),
    target_branch_policy: {
      ...(typeof policy['branch_template'] === 'string'
        ? { branch_template: policy['branch_template'] }
        : {}),
      ...(typeof policy['merge_policy'] === 'string'
        ? { merge_policy: policy['merge_policy'] as 'proposal' }
        : {}),
    },
    ...(Object.keys(extra).length === 0 ? {} : { extra }),
  };
}

/** The view `Schema` of a `SchemaOut`. */
export function mapSchema(out: SchemaOut): Schema {
  const body: Raw = out.schema;
  const names = new Names();
  for (const section of ['kinds', 'relations', 'traits', 'operations', 'derivations', 'sources']) {
    for (const element of entries(body[section])) {
      names.add(element);
      for (const owned of entries(element['attributes'])) names.add(owned);
    }
  }
  const info = isRaw(body['domain']) ? body['domain'] : {};
  const settings = isRaw(info['settings']) ? info['settings'] : undefined;
  return {
    domain: out.domain,
    domain_info: {
      ...(typeof info['display_name'] === 'string' ? { display_name: info['display_name'] } : {}),
      ...(typeof info['description'] === 'string' ? { description: info['description'] } : {}),
      ...(settings === undefined ? {} : { settings }),
    },
    branch: out.branch ?? '',
    revision: out.revision,
    kinds: entries(body['kinds']).map((k) => kind(k, names)),
    relations: entries(body['relations']).map((r) => relation(r, names)),
    traits: entries(body['traits']).map((t) => trait(t, names)),
    operations: entries(body['operations']).map((o) => operation(o, names)),
    derivations: entries(body['derivations']).map((d) => derivation(d, names)),
    sources: entries(body['sources']).map(source),
    search: { modes: [...out.search.modes], embedding: out.search.embedding },
  };
}
