/**
 * Structural rules of a schema (design 5), checked the same way by the Modeler while a
 * person edits and by the mock adapter before it accepts a change set. Issues carry a
 * path in the layout of the YAML document: `kinds/Supplier/attributes/tier/type`.
 */
import type { Attribute, JsonValue, Schema, Sensitivity } from './types';
import { parseAttributeType, validateValue } from './values';

export type SchemaBody = Pick<
  Schema,
  'kinds' | 'relations' | 'traits' | 'operations' | 'derivations' | 'sources'
>;

export interface SchemaIssue {
  path: string;
  code: string;
  message: string;
}

const KIND_NAME = /^[A-Z][A-Za-z0-9]*$/;
const SNAKE_NAME = /^[a-z][a-z0-9_]*$/;
const SOURCE_NAME = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/;
const SECRET_REFERENCE = /^secret:\/\/\S+$/;

export const CARDINALITIES = ['one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'] as const;
export const ON_DELETE = ['cascade', 'restrict'] as const;
export const SENSITIVITIES: readonly Sensitivity[] = ['pii', 'confidential', 'restricted'];
export const OPERATION_ACTIONS = [
  'create_record',
  'update_record',
  'delete_record',
  'create_link',
  'delete_link',
] as const;

class Collector {
  readonly issues: SchemaIssue[] = [];

  add(path: string, code: string, message: string) {
    this.issues.push({ path, code, message });
  }

  /** Report names that appear more than once in `names`, at `pathOf(name)`. */
  duplicates(names: string[], pathOf: (name: string) => string, what: string) {
    const seen = new Set<string>();
    for (const name of names) {
      if (seen.has(name))
        this.add(pathOf(name), 'duplicate_name', `${what} ${name} is defined twice.`);
      seen.add(name);
    }
  }
}

function checkAttribute(
  out: Collector,
  path: string,
  attribute: Attribute,
  kindNames: ReadonlySet<string>,
) {
  if (!SNAKE_NAME.test(attribute.api_name)) {
    out.add(path, 'invalid_name', `Attribute name ${attribute.api_name} must be snake_case.`);
  }
  const parsed = parseAttributeType(attribute.type);
  if (parsed === null) {
    out.add(`${path}/type`, 'unknown_type', `Unknown type ${attribute.type}.`);
    return;
  }
  if (parsed.base === 'ref' && parsed.ref !== undefined && !kindNames.has(parsed.ref)) {
    out.add(`${path}/type`, 'unknown_reference', `Kind ${parsed.ref} does not exist.`);
  }
  const { constraints } = attribute;
  if (parsed.base === 'enum' && (constraints.enum_values ?? []).length === 0) {
    out.add(`${path}/type`, 'empty_enum', 'An enum needs at least one value.');
  }
  if (
    constraints.min !== undefined &&
    constraints.max !== undefined &&
    constraints.min > constraints.max
  ) {
    out.add(`${path}/constraints`, 'invalid_range', 'min is greater than max.');
  }
  if (
    constraints.min_length !== undefined &&
    constraints.max_length !== undefined &&
    constraints.min_length > constraints.max_length
  ) {
    out.add(`${path}/constraints`, 'invalid_range', 'min_length is greater than max_length.');
  }
  if (constraints.pattern !== undefined) {
    try {
      new RegExp(constraints.pattern);
    } catch {
      out.add(
        `${path}/constraints/pattern`,
        'invalid_pattern',
        'The pattern is not a valid expression.',
      );
    }
  }
  if (attribute.default !== null && attribute.default !== undefined) {
    const problem = validateValue(
      { type: attribute.type, constraints },
      attribute.default as JsonValue,
    );
    if (problem !== null) out.add(`${path}/default`, 'invalid_default', problem);
  }
  for (const tag of attribute.sensitivity) {
    if (!SENSITIVITIES.includes(tag)) {
      out.add(`${path}/sensitivity`, 'unknown_sensitivity', `Unknown sensitivity ${tag}.`);
    }
  }
}

function checkKinds(out: Collector, schema: SchemaBody, kindNames: ReadonlySet<string>) {
  out.duplicates(
    schema.kinds.map((k) => k.api_name),
    (name) => `kinds/${name}`,
    'Kind',
  );
  for (const kind of schema.kinds) {
    const path = `kinds/${kind.api_name}`;
    if (!KIND_NAME.test(kind.api_name)) {
      out.add(path, 'invalid_name', `Kind name ${kind.api_name} must be PascalCase.`);
    }
    const attributes = Object.values(kind.attributes);
    out.duplicates(
      attributes.map((a) => a.api_name),
      (name) => `${path}/attributes/${name}`,
      'Attribute',
    );
    for (const attribute of attributes) {
      checkAttribute(out, `${path}/attributes/${attribute.api_name}`, attribute, kindNames);
    }
    for (const id of kind.primary_key) {
      if (kind.attributes[id] === undefined) {
        out.add(
          `${path}/primary_key`,
          'unknown_attribute',
          `Primary key Attribute ${id} does not exist.`,
        );
      }
    }
    if (kind.attributes[kind.title_attribute] === undefined) {
      out.add(
        `${path}/title_attribute`,
        'unknown_attribute',
        'The title Attribute must be one of the Kind Attributes.',
      );
    }
    kind.traits.forEach((implementation, index) => {
      const trait = schema.traits.find((t) => t.id === implementation.trait_id);
      const traitPath = `${path}/traits/${index}`;
      if (trait === undefined) {
        out.add(traitPath, 'unknown_reference', `Trait ${implementation.trait_id} does not exist.`);
        return;
      }
      for (const required of trait.attributes) {
        const mapped = kind.attributes[implementation.attribute_map[required.id] ?? ''];
        if (mapped === undefined) {
          if (required.required) {
            out.add(
              `${traitPath}/attribute_map`,
              'unmapped_trait_attribute',
              `${trait.api_name}.${required.name} is not mapped to an Attribute of ${kind.api_name}.`,
            );
          }
        } else if (mapped.type !== required.type) {
          out.add(
            `${traitPath}/attribute_map`,
            'trait_type_mismatch',
            `${mapped.api_name} is ${mapped.type} but ${trait.api_name}.${required.name} is ${required.type}.`,
          );
        }
      }
    });
  }
}

function checkRelations(out: Collector, schema: SchemaBody, kindNames: ReadonlySet<string>) {
  const names = schema.relations.flatMap((r) => [r.api_name, r.inverse_api_name]);
  out.duplicates(names, (name) => `relations/${name}`, 'Relation name');
  for (const relation of schema.relations) {
    const path = `relations/${relation.api_name}`;
    for (const name of [relation.api_name, relation.inverse_api_name]) {
      if (!SNAKE_NAME.test(name))
        out.add(path, 'invalid_name', `Relation name ${name} must be snake_case.`);
    }
    if (relation.api_name === relation.inverse_api_name) {
      out.add(
        `${path}/inverse_api_name`,
        'invalid_inverse',
        'The inverse name must differ from the forward name.',
      );
    }
    const from = schema.kinds.find((k) => k.id === relation.from_kind);
    for (const [field, id] of [
      ['from_kind', relation.from_kind],
      ['to_kind', relation.to_kind],
    ] as const) {
      if (!schema.kinds.some((k) => k.id === id)) {
        out.add(`${path}/${field}`, 'unknown_reference', `Kind ${id} does not exist.`);
      }
    }
    if (!CARDINALITIES.includes(relation.cardinality)) {
      out.add(
        `${path}/cardinality`,
        'invalid_cardinality',
        `Unknown cardinality ${relation.cardinality}.`,
      );
    }
    if (!ON_DELETE.includes(relation.on_delete)) {
      out.add(
        `${path}/on_delete`,
        'invalid_on_delete',
        `Unknown delete behavior ${relation.on_delete}.`,
      );
    }
    if (relation.backing === 'foreign_key') {
      const attribute = relation.foreign_key_attribute ?? '';
      if (from?.attributes[attribute] === undefined) {
        out.add(
          `${path}/foreign_key_attribute`,
          'unknown_attribute',
          'A foreign key Relation names an Attribute of its from Kind.',
        );
      }
    }
    for (const attribute of Object.values(relation.attributes)) {
      checkAttribute(out, `${path}/attributes/${attribute.api_name}`, attribute, kindNames);
    }
  }
}

function checkTraits(out: Collector, schema: SchemaBody) {
  out.duplicates(
    schema.traits.map((t) => t.api_name),
    (name) => `traits/${name}`,
    'Trait',
  );
  for (const trait of schema.traits) {
    const path = `traits/${trait.api_name}`;
    if (!KIND_NAME.test(trait.api_name)) {
      out.add(path, 'invalid_name', `Trait name ${trait.api_name} must be PascalCase.`);
    }
    out.duplicates(
      trait.attributes.map((a) => a.name),
      (name) => `${path}/attributes/${name}`,
      'Trait Attribute',
    );
    for (const attribute of trait.attributes) {
      if (parseAttributeType(attribute.type) === null) {
        out.add(
          `${path}/attributes/${attribute.name}/type`,
          'unknown_type',
          `Unknown type ${attribute.type}.`,
        );
      }
    }
  }
}

function checkOperations(out: Collector, schema: SchemaBody, kindNames: ReadonlySet<string>) {
  const custom = schema.operations.filter((o) => !o.builtin);
  out.duplicates(
    custom.map((o) => o.api_name),
    (name) => `operations/${name}`,
    'Operation',
  );
  const relationNames = new Set(schema.relations.map((r) => r.api_name));
  for (const operation of custom) {
    const path = `operations/${operation.api_name}`;
    if (!SNAKE_NAME.test(operation.api_name)) {
      out.add(path, 'invalid_name', `Operation name ${operation.api_name} must be snake_case.`);
    }
    out.duplicates(
      operation.parameters.map((p) => p.name),
      (name) => `${path}/parameters/${name}`,
      'Parameter',
    );
    operation.parameters.forEach((parameter, index) => {
      const known = parameter.type === 'ref' || parseAttributeType(parameter.type) !== null;
      if (!known)
        out.add(
          `${path}/parameters/${index}/type`,
          'unknown_type',
          `Unknown type ${parameter.type}.`,
        );
      if (parameter.kind_ref !== undefined && !kindNames.has(parameter.kind_ref)) {
        out.add(
          `${path}/parameters/${index}/type`,
          'unknown_reference',
          `Kind ${parameter.kind_ref} does not exist.`,
        );
      }
    });
    if (operation.steps.length === 0)
      out.add(`${path}/steps`, 'no_steps', 'An Operation needs at least one step.');
    operation.steps.forEach((step, index) => {
      const stepPath = `${path}/steps/${index}`;
      if (!OPERATION_ACTIONS.some((action) => action === step.action)) {
        out.add(stepPath, 'unknown_step', `Unknown step ${step.action}.`);
      }
      const kind = step.with['kind'];
      if (typeof kind === 'string' && !kindNames.has(kind)) {
        out.add(`${stepPath}/kind`, 'unknown_reference', `Kind ${kind} does not exist.`);
      }
      const relation = step.with['relation'];
      if (typeof relation === 'string' && !relationNames.has(relation)) {
        out.add(
          `${stepPath}/relation`,
          'unknown_reference',
          `Relation ${relation} does not exist.`,
        );
      }
    });
    if (operation.review_policy.trim() === '') {
      out.add(
        `${path}/review_policy`,
        'missing_review_policy',
        'The review policy is never, always or an expression.',
      );
    }
  }
}

function checkDerivationsAndSources(out: Collector, schema: SchemaBody) {
  for (const derivation of schema.derivations) {
    const path = `derivations/${derivation.api_name}`;
    if (!SNAKE_NAME.test(derivation.api_name)) {
      out.add(path, 'invalid_name', `Derivation name ${derivation.api_name} must be snake_case.`);
    }
    if (!schema.kinds.some((k) => k.id === derivation.target.kind_id)) {
      out.add(
        `${path}/target`,
        'unknown_reference',
        `Kind ${derivation.target.kind_id} does not exist.`,
      );
    }
  }
  for (const source of schema.sources) {
    const path = `sources/${source.api_name}`;
    if (!SOURCE_NAME.test(source.api_name)) {
      out.add(path, 'invalid_name', `Source name ${source.api_name} must be dotted snake_case.`);
    }
    if (!schema.kinds.some((k) => k.id === source.target_kind)) {
      out.add(
        `${path}/target_kind`,
        'unknown_reference',
        `Kind ${source.target_kind} does not exist.`,
      );
    }
    if (!SECRET_REFERENCE.test(source.connection)) {
      out.add(
        `${path}/connection`,
        'literal_secret',
        'A connection is a secret:// reference, never a literal.',
      );
    }
  }
}

/** Every rule violation of `schema`, in a stable order. */
export function validateSchema(schema: SchemaBody): SchemaIssue[] {
  const out = new Collector();
  const kindNames = new Set(schema.kinds.map((k) => k.api_name));
  checkKinds(out, schema, kindNames);
  checkRelations(out, schema, kindNames);
  checkTraits(out, schema);
  checkOperations(out, schema, kindNames);
  checkDerivationsAndSources(out, schema);
  return out.issues;
}
