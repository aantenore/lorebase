import { asRemote, createMockApi } from '@/api/mock/adapter';
import type { Schema } from '@/api/types';
import { documentToSchema, schemaToDocument, type IdFactory } from './document';
import { dumpSchemaYaml, locateIssue, parseSchemaYaml } from './yaml';

const mint: IdFactory = (prefix, key) => `${prefix}_new_${key.replace(/\W+/g, '_')}`;

async function sample(branch: string): Promise<Schema> {
  const api = asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
  return api.schema.get({ domain: 'supply', branch });
}

const custom = (schema: Schema) => schema.operations.filter((o) => !o.builtin);

describe('schema document', () => {
  it.each(['main', 'model/risk'])(
    'round-trips the %s schema through the document',
    async (branch) => {
      const schema = await sample(branch);
      const { schema: back, issues } = documentToSchema(schemaToDocument(schema), schema, mint);
      expect(issues).toEqual([]);
      expect(back.kinds).toEqual(schema.kinds);
      expect(back.relations).toEqual(schema.relations);
      expect(back.traits).toEqual(schema.traits);
      expect(back.derivations).toEqual(schema.derivations);
      expect(back.sources).toEqual(schema.sources);
      expect(custom(back)).toEqual(custom(schema));
    },
  );

  it('round-trips through YAML text', async () => {
    const schema = await sample('model/risk');
    const text = dumpSchemaYaml(schemaToDocument(schema));
    expect(text).toContain('format: lorebase.schema/v1');
    const parsed = parseSchemaYaml(text);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const { schema: back } = documentToSchema(parsed.document, schema, mint);
    expect(back.kinds).toEqual(schema.kinds);
    expect(custom(back)).toEqual(custom(schema));
  });

  it('writes elements by api name with references by api name and ids always', async () => {
    const document = schemaToDocument(await sample('main'));
    const supplier = document.kinds?.['Supplier'];
    expect(supplier?.id).toBe('knd_supplier');
    expect(supplier?.primary_key).toEqual(['supplier_code']);
    expect(supplier?.attributes?.['tier']?.type).toEqual({ enum: ['1', '2', '3'] });
    expect(supplier?.traits).toEqual([
      { trait: 'Located', attribute_map: { country: 'country', city: 'city' } },
    ]);
    expect(document.relations?.['supplied_by']).toMatchObject({
      from_kind: 'Part',
      to_kind: 'Supplier',
      cardinality: 'many_to_many',
    });
    expect(document.kinds?.['Supplier']?.attributes?.['tier']?.authority).toBe('erp.vendors');
  });

  it('reuses the id of the element with the same api name, and mints ids for new ones', async () => {
    const schema = await sample('main');
    const document = schemaToDocument(schema);
    delete document.kinds?.['Plant']?.id;
    document.kinds = {
      ...document.kinds,
      Warehouse: {
        attributes: { code: { type: 'string' } },
        primary_key: ['code'],
        title_attribute: 'code',
      },
    };
    const { schema: next, issues } = documentToSchema(document, schema, mint);
    expect(issues).toEqual([]);
    expect(next.kinds.find((k) => k.api_name === 'Plant')?.id).toBe('knd_plant');
    const warehouse = next.kinds.find((k) => k.api_name === 'Warehouse');
    expect(warehouse?.id).toBe('knd_new_kinds_Warehouse');
    expect(Object.keys(warehouse?.attributes ?? {})).toEqual(['att_new_kinds_Warehouse_code']);
  });

  it('keeps the id when an element is renamed with its id', async () => {
    const schema = await sample('main');
    const document = schemaToDocument(schema);
    const part = document.kinds?.['Part'];
    if (document.kinds === undefined || part === undefined) throw new Error('No Part');
    document.kinds = Object.fromEntries(
      Object.entries(document.kinds).map(([name, kind]) => [
        name === 'Part' ? 'Component' : name,
        kind,
      ]),
    );
    document.relations = {
      ...document.relations,
      supplied_by: { ...document.relations?.['supplied_by'], from_kind: 'Component' } as never,
    };
    const { schema: next, issues } = documentToSchema(document, schema, mint);
    // A rename does not rewrite the other documents that name the Kind; the stale one is reported.
    expect(issues.map((i) => i.path)).toEqual(['derivations/single_sourced/definition/kind']);
    expect(next.kinds.find((k) => k.id === 'knd_part')?.api_name).toBe('Component');
    expect(next.relations.find((r) => r.api_name === 'supplied_by')?.from_kind).toBe('knd_part');
  });

  it('reports what cannot be read with its path and keeps reading the rest', async () => {
    const schema = await sample('main');
    const { schema: next, issues } = documentToSchema(
      {
        kinds: { Broken: 'oops', Fine: { attributes: { name: { type: 'moneyish' } } } },
        relations: { r: { inverse_api_name: 'i', from_kind: 'Fine', to_kind: 'Nowhere' } },
      },
      schema,
      mint,
    );
    expect(issues.map((i) => `${i.code} ${i.path}`)).toEqual([
      'invalid_document kinds/Broken',
      'unknown_type kinds/Fine/attributes/name/type',
      'unknown_reference relations/r/to_kind',
    ]);
    expect(next.kinds.map((k) => k.api_name)).toEqual(['Fine']);
  });
});

describe('schema YAML', () => {
  it('reports syntax errors with line and column', () => {
    const parsed = parseSchemaYaml('kinds:\n  Supplier:\n    attributes: [oops\n');
    expect(parsed.ok).toBe(false);
    if (parsed.ok) return;
    expect(parsed.issues[0]?.code).toBe('yaml_syntax');
    expect(parsed.issues[0]?.line).toBeGreaterThan(0);
  });

  it('finds the line of a document path', () => {
    const text = 'kinds:\n  Supplier:\n    attributes:\n      tier:\n        type: string\n';
    expect(locateIssue(text, 'kinds/Supplier/attributes/tier/type')?.line).toBe(5);
    expect(locateIssue(text, 'kinds/Supplier/attributes/missing')?.line).toBe(4);
  });
});
