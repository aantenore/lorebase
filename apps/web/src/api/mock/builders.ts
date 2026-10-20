import type {
  Attribute,
  AttributeConstraints,
  AttributeType,
  Cardinality,
  Kind,
  Operation,
  Relation,
  Revision,
  RevisionOriginType,
  SearchWeight,
  Trait,
  TraitAttribute,
} from '../types';
import { refOf, type StoredLink, type StoredRecord } from './state';

export const eid = (prefix: string, name: string) => `${prefix}_${name}`;

interface AttrOptions {
  description?: string;
  required?: boolean;
  unique?: boolean;
  constraints?: AttributeConstraints;
  indexed?: boolean;
  searchable?: SearchWeight;
  sensitivity?: Attribute['sensitivity'];
  authority?: string;
}

export function attr(
  scope: string,
  apiName: string,
  type: AttributeType,
  options: AttrOptions = {},
): Attribute {
  return {
    id: eid('att', `${scope}_${apiName}`),
    api_name: apiName,
    display_name: apiName.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()),
    description: options.description ?? '',
    type,
    required: options.required ?? false,
    unique: options.unique ?? false,
    default: null,
    constraints: options.constraints ?? {},
    sensitivity: options.sensitivity ?? [],
    indexed: options.indexed ?? false,
    searchable: options.searchable ?? null,
    derivation_id: null,
    authority: options.authority ?? null,
  };
}

interface KindOptions {
  description: string;
  key: string;
  title: string;
  color: string;
  traits?: Kind['traits'];
}

export function kind(apiName: string, attributes: Attribute[], options: KindOptions): Kind {
  const byName = (name: string) => {
    const found = attributes.find((a) => a.api_name === name);
    if (found === undefined) throw new Error(`Unknown Attribute ${apiName}.${name}`);
    return found.id;
  };
  return {
    id: eid('knd', apiName.toLowerCase()),
    api_name: apiName,
    display_name: apiName,
    description: options.description,
    primary_key: [byName(options.key)],
    title_attribute: byName(options.title),
    attributes: Object.fromEntries(attributes.map((a) => [a.id, a])),
    traits: options.traits ?? [],
    status: 'active',
    search: {
      text_attributes: attributes
        .filter((a) => a.searchable !== null)
        .map((a) => ({ attribute_id: a.id, weight: a.searchable ?? 'D' })),
      embedding_template: null,
    },
    icon: null,
    color: options.color,
    operations_builtin: { create: true, update: true, delete: true },
  };
}

interface RelationOptions {
  inverse: string;
  from: Kind;
  to: Kind;
  cardinality: Cardinality;
  description: string;
  attributes?: Attribute[];
  requiredFrom?: boolean;
}

export function relation(apiName: string, options: RelationOptions): Relation {
  const attributes = options.attributes ?? [];
  return {
    id: eid('rel', apiName),
    api_name: apiName,
    inverse_api_name: options.inverse,
    display_name: apiName.replace(/_/g, ' '),
    description: options.description,
    from_kind: options.from.id,
    to_kind: options.to.id,
    cardinality: options.cardinality,
    required_from: options.requiredFrom ?? false,
    attributes: Object.fromEntries(attributes.map((a) => [a.id, a])),
    on_delete: 'cascade',
    backing: 'explicit',
  };
}

export function trait(apiName: string, description: string, attributes: TraitAttribute[]): Trait {
  return {
    id: eid('trt', apiName.toLowerCase()),
    api_name: apiName,
    display_name: apiName,
    description,
    attributes,
  };
}

/** Built-in Operations that Keeper generates for every Kind (design 5.7). */
export function builtinOperations(kinds: Kind[]): Operation[] {
  return kinds.flatMap((k) =>
    (['create', 'update', 'delete'] as const).map((verb): Operation => {
      const name = `${verb}_${k.api_name.toLowerCase()}`;
      return {
        id: eid('opr', name),
        api_name: name,
        display_name: name,
        description: `${verb[0]?.toUpperCase() ?? ''}${verb.slice(1)} a ${k.api_name} Record.`,
        parameters: [],
        preconditions: [],
        steps: [{ action: `${verb}_record`, with: { kind: k.api_name } }],
        review_policy: 'never',
        permissions: ['editor'],
        side_effects: [],
        rate_limit: null,
        status: 'active',
        builtin: true,
      };
    }),
  );
}

export function record(
  kindName: string,
  key: string,
  values: Record<string, unknown>,
  derived: Record<string, unknown> = {},
): StoredRecord {
  return { kind: kindName, key, values, derived };
}

export function link(
  relationName: string,
  from: [string, string],
  to: [string, string],
  values: Record<string, unknown> = {},
): StoredLink {
  return { relation: relationName, from: refOf(...from), to: refOf(...to), values };
}

export function revision(
  domain: string,
  id: string,
  parent: string | null,
  message: string,
  createdAt: string,
  originType: RevisionOriginType,
  options: { author?: string; changes?: number; originRef?: string | null } = {},
): Revision {
  return {
    id,
    parents: parent === null ? [] : [parent],
    root: `root_${id}`,
    domain,
    author: { principal: options.author ?? 'usr_marco', on_behalf_of: null },
    message,
    origin: { type: originType, ref: options.originRef ?? null },
    created_at: createdAt,
    stats: { partitions_changed: 1, changes: options.changes ?? 1 },
  };
}
