import { asRemote, createMockApi } from './mock/adapter';
import { applySchemaChanges, diffSchemas } from './schema-changes';
import { validateSchema } from './schema-rules';
import type { Schema } from './types';
import { castRule, castValue, parseAttributeType, validateValue, type ValueSpec } from './values';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

async function sampleSchema(branch: string): Promise<Schema> {
  const api = asRemote(createMockApi(options), 0);
  return api.schema.get({ domain: 'supply', branch });
}

describe('value checks', () => {
  it('parses the spellings of a type', () => {
    expect(parseAttributeType('string')).toEqual({ base: 'string', array: false });
    expect(parseAttributeType('array<enum>')).toEqual({ base: 'enum', array: true });
    expect(parseAttributeType('decimal(12,2)')).toMatchObject({ base: 'decimal', precision: 12 });
    expect(parseAttributeType('ref(Supplier)')).toMatchObject({ base: 'ref', ref: 'Supplier' });
    expect(parseAttributeType('array<array<string>>')).toBeNull();
    expect(parseAttributeType('money')).toBeNull();
  });

  it('validates values against type and constraints', () => {
    const score: ValueSpec = { type: 'integer', constraints: { min: 0, max: 60 } };
    expect(validateValue(score, 42)).toBeNull();
    expect(validateValue(score, 71)).toBe('Must be at most 60.');
    expect(validateValue(score, 1.5)).toBe('Must be a whole number.');
    expect(validateValue({ type: 'integer', required: true }, null)).toBe('A value is required.');
    expect(validateValue({ type: 'integer' }, null)).toBeNull();
    const tier: ValueSpec = { type: 'enum', constraints: { enum_values: ['1', '2', '3'] } };
    expect(validateValue(tier, '2')).toBeNull();
    expect(validateValue(tier, '9')).toBe('Must be one of: 1, 2, 3.');
    expect(validateValue({ type: 'date' }, '2026-02-30')).not.toBeNull();
    expect(validateValue({ type: 'array<string>' }, ['a', 'b'])).toBeNull();
    expect(validateValue({ type: 'array<string>' }, 'a')).toBe('Must be a list.');
  });

  it('classifies casts and applies them value by value', () => {
    expect(castRule('integer', 'decimal')).toBe('safe');
    expect(castRule('string', 'integer')).toBe('checked');
    expect(castRule('geo_point', 'boolean')).toBe('forbidden');
    expect(castValue('42', 'string', 'integer')).toEqual({ ok: true, value: 42 });
    expect(castValue('forty', 'string', 'integer')).toEqual({ ok: false });
    expect(castValue(7, 'integer', 'string')).toEqual({ ok: true, value: '7' });
  });
});

describe('schema rules', () => {
  it('accept the sample schemas of every Branch', async () => {
    expect(validateSchema(await sampleSchema('main'))).toEqual([]);
    expect(validateSchema(await sampleSchema('model/risk'))).toEqual([]);
  });

  it('point at the offending element with a YAML path', async () => {
    const schema = await sampleSchema('main');
    const supplier = schema.kinds[0];
    if (supplier === undefined) throw new Error('No Kind');
    supplier.primary_key = ['att_missing'];
    const relation = schema.relations[0];
    if (relation === undefined) throw new Error('No Relation');
    relation.to_kind = 'knd_missing';
    const source = schema.sources[0];
    if (source === undefined) throw new Error('No Source');
    source.connection = 'postgres://user:pw@host/db';
    const paths = validateSchema(schema).map((i) => i.path);
    expect(paths).toEqual(
      expect.arrayContaining([
        'kinds/Supplier/primary_key',
        'relations/supplied_by/to_kind',
        'sources/erp.vendors/connection',
      ]),
    );
  });
});

describe('schema change commands', () => {
  it('apply back to the schema they were computed from', async () => {
    const before = await sampleSchema('model/risk');
    const after = structuredClone(before);
    const part = after.kinds.find((k) => k.api_name === 'Part');
    if (part === undefined) throw new Error('No Part');
    part.api_name = 'Component';
    part.description = 'A bought component.';
    const supplier = after.kinds.find((k) => k.api_name === 'Supplier');
    const city = Object.values(supplier?.attributes ?? {}).find((a) => a.api_name === 'city');
    if (supplier === undefined || city === undefined) throw new Error('No Supplier city');
    city.indexed = true;

    delete supplier.attributes['att_supplier_tier'];
    after.relations = after.relations.filter((r) => r.api_name !== 'feeds');
    after.traits[0]!.description = 'Changed.';

    const changes = diffSchemas(before, after);
    expect(changes.map((c) => c.command).sort()).toEqual([
      'remove_attribute',
      'remove_relation',
      'update_attribute',
      'update_kind',
      'update_trait',
    ]);
    const applied = applySchemaChanges(before, changes);
    expect(applied.kinds).toEqual(after.kinds);
    expect(applied.relations).toEqual(after.relations);
    expect(applied.traits).toEqual(after.traits);
  });

  it('are empty when nothing changed and ignore built-in Operations', async () => {
    const schema = await sampleSchema('main');
    expect(diffSchemas(schema, structuredClone(schema))).toEqual([]);
    const withoutBuiltins = { ...schema, operations: schema.operations.filter((o) => !o.builtin) };
    expect(diffSchemas(schema, withoutBuiltins)).toEqual([]);
  });
});
