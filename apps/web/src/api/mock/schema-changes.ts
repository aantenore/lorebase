/**
 * Schema change sets of the mock adapter (design 6.6, 5.4): apply the commands to the
 * Branch schema, check the structural rules, migrate the stored Records and Links
 * (casts, defaults, dropped Attributes) and report what the change set implies.
 */
import { problem } from '../errors';
import { applySchemaChanges, SchemaChangeError } from '../schema-changes';
import { validateSchema } from '../schema-rules';
import type {
  Attribute,
  JsonValue,
  Kind,
  SchemaChange,
  SchemaMigration,
  Violation,
} from '../types';
import { castValue, validateValue } from '../values';
import { builtinOperations } from './builders';
import { refOf, splitRef, type BranchData, type StoredLink, type StoredRecord } from './state';

export interface Evaluation {
  valid: boolean;
  violations: Violation[];
  migrations: SchemaMigration[];
  /** The Branch state after the change set, with the migrated Records and Links. */
  next: BranchData;
}

/** Keep derived structures in step with the Attributes: search settings, built-in Operations. */
function normalize(schema: BranchData['schema'], previous: BranchData['schema']) {
  for (const kind of schema.kinds) {
    kind.search.text_attributes = Object.values(kind.attributes)
      .filter((a) => a.searchable !== null)
      .map((a) => ({ attribute_id: a.id, weight: a.searchable ?? 'D' }));
  }
  const existing = new Map(previous.operations.map((o) => [o.id, o]));
  const builtin = builtinOperations(schema.kinds).map((o) => existing.get(o.id) ?? o);
  schema.operations = [...builtin, ...schema.operations.filter((o) => !o.builtin)];
}

interface CastStat {
  label: string;
  to: string;
  affected: number;
  failing: number;
  path: string;
}

class Counter {
  private readonly counts = new Map<string, number>();

  bump(key: string) {
    this.counts.set(key, (this.counts.get(key) ?? 0) + 1);
  }

  entries() {
    return [...this.counts];
  }
}

function attributeByName(kind: Kind, name: string): Attribute | undefined {
  return Object.values(kind.attributes).find((a) => a.api_name === name);
}

function migrateRecords(
  before: BranchData['schema'],
  after: BranchData['schema'],
  records: StoredRecord[],
): {
  records: StoredRecord[];
  dropped: Set<string>;
  renames: Map<string, string>;
  migrations: SchemaMigration[];
  castStats: Map<string, CastStat>;
} {
  const nextKinds = new Map(after.kinds.map((k) => [k.id, k]));
  const oldKinds = new Map(before.kinds.map((k) => [k.api_name, k]));
  const renames = new Map<string, string>();
  const droppedKinds = new Counter();
  const droppedValues = new Counter();
  const defaults = new Counter();
  const castStats = new Map<string, CastStat>();
  const dropped = new Set<string>();
  const out: StoredRecord[] = [];

  for (const record of records) {
    const oldKind = oldKinds.get(record.kind);
    const nextKind = oldKind === undefined ? undefined : nextKinds.get(oldKind.id);
    if (oldKind === undefined) {
      out.push(record);
      continue;
    }
    if (nextKind === undefined) {
      droppedKinds.bump(oldKind.api_name);
      dropped.add(refOf(record.kind, record.key));
      continue;
    }
    renames.set(oldKind.api_name, nextKind.api_name);
    const values: Record<string, unknown> = {};
    for (const [name, value] of Object.entries(record.values)) {
      const was = attributeByName(oldKind, name);
      if (was === undefined) {
        values[name] = value;
        continue;
      }
      const now = nextKind.attributes[was.id];
      if (now === undefined) {
        droppedValues.bump(`${oldKind.api_name}.${was.api_name}`);
        continue;
      }
      if (now.type === was.type || value === null || value === undefined) {
        values[now.api_name] = value;
        continue;
      }
      const stat = castStats.get(was.id) ?? {
        label: `${nextKind.api_name}.${now.api_name}`,
        to: `${was.type} to ${now.type}`,
        affected: 0,
        failing: 0,
        path: `kinds/${nextKind.api_name}/attributes/${now.api_name}/type`,
      };
      stat.affected += 1;
      const cast = castValue(value as JsonValue, was.type, now.type, now.constraints);
      if (cast.ok) values[now.api_name] = cast.value;
      else {
        stat.failing += 1;
        values[now.api_name] = value;
      }
      castStats.set(was.id, stat);
    }
    for (const now of Object.values(nextKind.attributes)) {
      const was = oldKind.attributes[now.id];
      const becameRequired = was !== undefined && !was.required && now.required;
      if (
        values[now.api_name] === undefined &&
        now.default !== null &&
        (was === undefined || becameRequired)
      ) {
        values[now.api_name] = now.default;
        defaults.bump(`${nextKind.api_name}.${now.api_name}`);
      }
    }
    out.push({ kind: nextKind.api_name, key: record.key, values, derived: record.derived });
  }

  const migrations: SchemaMigration[] = [
    ...droppedKinds.entries().map(([name, n]) => ({
      description: `Drop ${n} Records of removed Kind ${name}`,
      affected_values: n,
      failing_values: 0,
    })),
    ...droppedValues.entries().map(([name, n]) => ({
      description: `Drop ${n} values of removed Attribute ${name}`,
      affected_values: n,
      failing_values: 0,
    })),
    ...[...castStats.values()].map((s) => ({
      description: `Cast ${s.label} from ${s.to}`,
      affected_values: s.affected,
      failing_values: s.failing,
    })),
    ...defaults.entries().map(([name, n]) => ({
      description: `Apply the default of ${name} to ${n} Records`,
      affected_values: n,
      failing_values: 0,
    })),
  ];
  return { records: out, dropped, renames, migrations, castStats };
}

function migrateLinks(
  before: BranchData['schema'],
  after: BranchData['schema'],
  links: StoredLink[],
  dropped: Set<string>,
  renames: Map<string, string>,
): { links: StoredLink[]; migrations: SchemaMigration[] } {
  const oldRelations = new Map(before.relations.map((r) => [r.api_name, r]));
  const nextRelations = new Map(after.relations.map((r) => [r.id, r]));
  const lost = new Counter();
  const rename = (ref: string) => {
    const { kind, key } = splitRef(ref);
    return refOf(renames.get(kind) ?? kind, key);
  };
  const out: StoredLink[] = [];
  for (const link of links) {
    const was = oldRelations.get(link.relation);
    const now = was === undefined ? undefined : nextRelations.get(was.id);
    if (was !== undefined && now === undefined) {
      lost.bump(was.api_name);
    } else if (dropped.has(link.from) || dropped.has(link.to)) {
      lost.bump(link.relation);
    } else {
      out.push({
        relation: now?.api_name ?? link.relation,
        from: rename(link.from),
        to: rename(link.to),
        values: link.values,
      });
    }
  }
  const migrations = lost.entries().map(([name, n]) => ({
    description: `Drop ${n} Links of ${name}`,
    affected_values: n,
    failing_values: 0,
  }));
  return { links: out, migrations };
}

/** Records that break the new schema, grouped per Attribute so one change set stays readable. */
function dataViolations(
  after: BranchData['schema'],
  records: StoredRecord[],
  skip: Set<string>,
): Violation[] {
  const violations: Violation[] = [];
  for (const kind of after.kinds) {
    const own = records.filter((r) => r.kind === kind.api_name);
    for (const attribute of Object.values(kind.attributes)) {
      if (skip.has(attribute.id) || attribute.derivation_id !== null) continue;
      const spec = {
        type: attribute.type,
        required: attribute.required,
        constraints: attribute.constraints,
      };
      const path = `kinds/${kind.api_name}/attributes/${attribute.api_name}`;
      const failing = own
        .map((r) => ({
          ref: refOf(r.kind, r.key),
          reason: validateValue(spec, (r.values[attribute.api_name] ?? null) as JsonValue),
        }))
        .filter((f) => f.reason !== null);
      const first = failing[0];
      if (first !== undefined) {
        violations.push({
          code: first.reason === 'A value is required.' ? 'value_required' : 'value_invalid',
          message: `${failing.length} Records violate ${kind.api_name}.${attribute.api_name}: ${first.reason ?? ''} First: ${first.ref}.`,
          ref: first.ref,
          path,
        });
      }
      if (attribute.unique) {
        const seen = new Set<unknown>();
        const duplicate = own.find((r) => {
          const value = r.values[attribute.api_name];
          if (value === undefined) return false;
          const repeated = seen.has(value);
          seen.add(value);
          return repeated;
        });
        if (duplicate !== undefined) {
          violations.push({
            code: 'not_unique',
            message: `${kind.api_name}.${attribute.api_name} has duplicate values, for example on ${refOf(duplicate.kind, duplicate.key)}.`,
            ref: refOf(duplicate.kind, duplicate.key),
            path,
          });
        }
      }
    }
  }
  return violations;
}

/**
 * Run a change set against a Branch without committing it. Throws a 422 problem when a
 * command does not apply; a change set that applies but breaks a rule is reported as not
 * valid, with the violations and the migrations it would need.
 */
export function evaluateSchemaChanges(data: BranchData, changes: SchemaChange[]): Evaluation {
  let schema: BranchData['schema'];
  try {
    schema = applySchemaChanges(data.schema, changes) as BranchData['schema'];
  } catch (error) {
    if (error instanceof SchemaChangeError) {
      throw problem(422, 'Invalid change set', error.message, 'invalid_change');
    }
    throw error;
  }
  normalize(schema, data.schema);

  const violations: Violation[] = validateSchema(schema).map((issue) => ({
    code: issue.code,
    message: issue.message,
    path: issue.path,
  }));
  const moved = migrateRecords(data.schema, schema, data.records);
  const linked = migrateLinks(data.schema, schema, data.links, moved.dropped, moved.renames);
  const castFailed = new Set<string>();
  for (const [id, stat] of moved.castStats) {
    if (stat.failing === 0) continue;
    castFailed.add(id);
    violations.push({
      code: 'cast_failed',
      message: `${stat.failing} of ${stat.affected} values of ${stat.label} cannot be cast (${stat.to}).`,
      path: stat.path,
    });
  }
  violations.push(...dataViolations(schema, moved.records, castFailed));

  return {
    valid: violations.length === 0,
    violations,
    migrations: [...moved.migrations, ...linked.migrations],
    next: { ...data, schema, records: moved.records, links: linked.links },
  };
}
