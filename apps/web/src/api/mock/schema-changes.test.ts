import type { KeeperApi } from '../client';
import { diffSchemas } from '../schema-changes';
import type { Attribute, Kind, Schema, SchemaChange } from '../types';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

const setup = (): KeeperApi => asRemote(createMockApi(options), 0);

const BRANCH = 'cleanup/tiers';
const read = (api: KeeperApi, branch: string) => api.schema.get({ domain: 'supply', branch });

const kindOf = (schema: Schema, name: string): Kind => {
  const found = schema.kinds.find((k) => k.api_name === name);
  if (found === undefined) throw new Error(`No Kind ${name}`);
  return found;
};

const attributeOf = (kind: Kind, name: string): Attribute => {
  const found = Object.values(kind.attributes).find((a) => a.api_name === name);
  if (found === undefined) throw new Error(`No Attribute ${name}`);
  return found;
};

/** Commands that turn the Branch schema into the one `edit` leaves behind. */
async function changesFor(
  api: KeeperApi,
  branch: string,
  edit: (schema: Schema) => void,
): Promise<SchemaChange[]> {
  const before = await read(api, branch);
  const after = structuredClone(before);
  edit(after);
  return diffSchemas(before, after);
}

const newAttribute = (id: string, name: string, patch: Partial<Attribute> = {}): Attribute => ({
  id,
  api_name: name,
  display_name: name,
  description: '',
  type: 'string',
  required: false,
  unique: false,
  default: null,
  constraints: {},
  sensitivity: [],
  indexed: false,
  searchable: null,
  derivation_id: null,
  authority: null,
  ...patch,
});

describe('mock schema change sets', () => {
  it('commits a change set on an unprotected Branch as a schema Revision', async () => {
    const api = setup();
    const before = await api.branches.get('supply', BRANCH);
    const changes = await changesFor(api, BRANCH, (schema) => {
      const supplier = kindOf(schema, 'Supplier');
      supplier.attributes['att_new_website'] = newAttribute('att_new_website', 'website', {
        type: 'uri',
      });
    });

    const result = await api.schema.applyChanges('supply', BRANCH, {
      changes,
      message: 'Add website',
    });
    expect(result).toMatchObject({ valid: true, violations: [] });
    expect(result.revision).not.toBeNull();

    const after = await api.branches.get('supply', BRANCH);
    expect(after.head).toBe(result.revision);
    expect(after.head).not.toBe(before.head);
    const log = await api.revisions.log('supply', BRANCH);
    expect(log.items[0]).toMatchObject({
      message: 'Add website',
      origin: { type: 'schema_change' },
    });
    const schema = await read(api, BRANCH);
    expect(attributeOf(kindOf(schema, 'Supplier'), 'website').type).toBe('uri');
  });

  it('refuses to commit on a protected Branch but allows a dry run', async () => {
    const api = setup();
    const changes = await changesFor(api, 'main', (schema) => {
      kindOf(schema, 'Supplier').description = 'Changed.';
    });
    await expect(api.schema.applyChanges('supply', 'main', { changes })).rejects.toMatchObject({
      status: 409,
      code: 'protected-branch',
    });
    const dry = await api.schema.applyChanges('supply', 'main', { changes, dry_run: true });
    expect(dry).toMatchObject({ valid: true, revision: null });
  });

  it('does not change anything on a dry run', async () => {
    const api = setup();
    const before = await api.branches.get('supply', BRANCH);
    const changes = await changesFor(api, BRANCH, (schema) => {
      kindOf(schema, 'Part').description = 'Changed.';
    });
    await api.schema.applyChanges('supply', BRANCH, { changes, dry_run: true });
    expect((await api.branches.get('supply', BRANCH)).head).toBe(before.head);
    expect(kindOf(await read(api, BRANCH), 'Part').description).not.toBe('Changed.');
  });

  it('reports a safe cast as a migration and applies it to the Records', async () => {
    const api = setup();
    const changes = await changesFor(api, BRANCH, (schema) => {
      attributeOf(kindOf(schema, 'Supplier'), 'risk_score').type = 'decimal';
    });
    const result = await api.schema.applyChanges('supply', BRANCH, { changes });
    expect(result.valid).toBe(true);
    expect(result.migrations).toEqual([
      expect.objectContaining({ affected_values: 9, failing_values: 0 }),
    ]);
    expect(result.migrations[0]?.description).toContain('Supplier.risk_score');
  });

  it('rejects a checked cast that fails on stored values and commits nothing', async () => {
    const api = setup();
    const before = await api.branches.get('supply', BRANCH);
    const changes = await changesFor(api, BRANCH, (schema) => {
      attributeOf(kindOf(schema, 'Supplier'), 'legal_name').type = 'integer';
    });
    const result = await api.schema.applyChanges('supply', BRANCH, { changes });
    expect(result.valid).toBe(false);
    expect(result.revision).toBeNull();
    expect(result.migrations[0]).toMatchObject({ affected_values: 9, failing_values: 9 });
    expect(result.violations).toEqual([
      expect.objectContaining({
        code: 'cast_failed',
        path: 'kinds/Supplier/attributes/legal_name/type',
      }),
    ]);
    expect((await api.branches.get('supply', BRANCH)).head).toBe(before.head);
  });

  it('drops the values of a removed Attribute and says how many', async () => {
    const api = setup();
    const changes = await changesFor(api, BRANCH, (schema) => {
      const supplier = kindOf(schema, 'Supplier');
      const city = attributeOf(supplier, 'city');

      delete supplier.attributes[city.id];
    });
    const result = await api.schema.applyChanges('supply', BRANCH, { changes });
    expect(result.valid).toBe(true);
    expect(result.migrations[0]?.description).toBe(
      'Drop 9 values of removed Attribute Supplier.city',
    );
    const record = await api.records.get(
      { domain: 'supply', branch: BRANCH },
      'Supplier',
      'S-1001',
    );
    expect(record.record.attributes).not.toHaveProperty('city');
  });

  it('renames an Attribute without losing its values', async () => {
    const api = setup();
    const changes = await changesFor(api, BRANCH, (schema) => {
      attributeOf(kindOf(schema, 'Supplier'), 'city').api_name = 'town';
    });
    await api.schema.applyChanges('supply', BRANCH, { changes });
    const record = await api.records.get(
      { domain: 'supply', branch: BRANCH },
      'Supplier',
      'S-1001',
    );
    expect(record.record.attributes['town']).toBe('Torino');
    expect(record.record.attributes).not.toHaveProperty('city');
  });

  it('requires a value for a new required Attribute unless it has a default', async () => {
    const api = setup();
    const withoutDefault = await changesFor(api, BRANCH, (schema) => {
      kindOf(schema, 'Part').attributes['att_new_grade'] = newAttribute('att_new_grade', 'grade', {
        required: true,
      });
    });
    const refused = await api.schema.applyChanges('supply', BRANCH, { changes: withoutDefault });
    expect(refused.valid).toBe(false);
    expect(refused.violations[0]).toMatchObject({
      code: 'value_required',
      path: 'kinds/Part/attributes/grade',
    });

    const withDefault = await changesFor(api, BRANCH, (schema) => {
      kindOf(schema, 'Part').attributes['att_new_grade'] = newAttribute('att_new_grade', 'grade', {
        required: true,
        default: 'B',
      });
    });
    const accepted = await api.schema.applyChanges('supply', BRANCH, { changes: withDefault });
    expect(accepted.valid).toBe(true);
    expect(accepted.migrations[0]).toMatchObject({ affected_values: 9 });
  });

  it('reports structural rule violations with their YAML path', async () => {
    const api = setup();
    const changes = await changesFor(api, BRANCH, (schema) => {
      const part = kindOf(schema, 'Part');
      attributeOf(part, 'name').api_name = 'Name';
      schema.relations[0]!.cardinality = 'several' as never;
    });
    const result = await api.schema.applyChanges('supply', BRANCH, { changes, dry_run: true });
    expect(result.valid).toBe(false);
    const paths = result.violations.map((v) => `${v.code} ${v.path ?? ''}`);
    expect(paths).toContain('invalid_name kinds/Part/attributes/Name');
    expect(paths).toContain('invalid_cardinality relations/supplied_by/cardinality');
  });

  it('generates the built-in Operations of a new Kind and drops the Links of a removed Relation', async () => {
    const api = setup();
    const added = await changesFor(api, BRANCH, (schema) => {
      const id = attributeOf(kindOf(schema, 'Plant'), 'name');
      schema.kinds.push({
        ...structuredClone(kindOf(schema, 'Plant')),
        id: 'knd_warehouse',
        api_name: 'Warehouse',
        display_name: 'Warehouse',
        primary_key: [id.id],
        title_attribute: id.id,
        traits: [],
      });
    });
    await api.schema.applyChanges('supply', BRANCH, { changes: added });
    const schema = await read(api, BRANCH);
    expect(schema.operations.map((o) => o.api_name)).toContain('create_warehouse');

    const removed = await changesFor(api, BRANCH, (next) => {
      next.relations = next.relations.filter((r) => r.api_name !== 'feeds');
    });
    const result = await api.schema.applyChanges('supply', BRANCH, { changes: removed });
    expect(result.migrations).toEqual([
      expect.objectContaining({ description: 'Drop 7 Links of feeds', affected_values: 7 }),
    ]);
  });

  it('answers a command that targets nothing with an invalid change problem', async () => {
    const api = setup();
    await expect(
      api.schema.applyChanges('supply', BRANCH, {
        changes: [{ command: 'remove_kind', target: 'knd_missing', arguments: {} }],
      }),
    ).rejects.toMatchObject({ status: 422, code: 'invalid_change' });
  });
});
