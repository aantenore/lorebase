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

  it('writes the embedding template under the Kind search block and reads it back', async () => {
    const schema = await sample('main');
    const kinds = schema.kinds.map((k) =>
      k.api_name === 'Supplier'
        ? {
            ...k,
            search: { ...k.search, embedding_template: '{legal_name}', embedding_sensitive: true },
          }
        : k,
    );
    const changed: Schema = { ...schema, kinds };
    const document = schemaToDocument(changed);
    expect(document.kinds?.['Supplier']?.search).toEqual({
      embedding_template: '{legal_name}',
      embedding_sensitive: true,
    });
    expect(document.kinds?.['Part']).not.toHaveProperty('search');
    const parsed = parseSchemaYaml(dumpSchemaYaml(document));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const { schema: back, issues } = documentToSchema(parsed.document, changed, mint);
    expect(issues).toEqual([]);
    expect(back.kinds).toEqual(changed.kinds);
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

const HTTP_OPTIONS = {
  headers: { Authorization: 'secret://env/API_TOKEN', Accept: 'application/json' },
  pagination: { type: 'cursor', cursor_path: 'meta.next', cursor_param: 'after', page_size: 100 },
  since_param: 'updated_since',
  max_pages: 20,
};

describe('source fields beyond the modelled ones', () => {
  /** The schema with `fields` merged into its first Source. */
  async function withSourceFields(fields: Record<string, unknown>): Promise<Schema> {
    const schema = await sample('main');
    const [first, ...rest] = schema.sources;
    if (first === undefined) throw new Error('No Source');
    return { ...schema, sources: [{ ...first, ...fields }, ...rest] };
  }

  it.each([
    ['table', { table: 'erp.vendors' }],
    ['query', { query: 'SELECT id, name FROM vendors' }],
    ['records_path', { records_path: 'data.items' }],
    ['flatten_separator', { flatten_separator: '__' }],
    ['http', { type: 'http_json', http: HTTP_OPTIONS }],
    ['an unknown key', { extra: { future_option: { nested: [1, 'two', true] }, retries: 3 } }],
  ])('keeps %s through the document', async (_name, fields) => {
    const schema = await withSourceFields(fields);
    const { schema: back, issues } = documentToSchema(schemaToDocument(schema), schema, mint);
    expect(issues).toEqual([]);
    expect(back.sources).toEqual(schema.sources);
    expect(back.sources[0]).toMatchObject(fields);
  });

  it('keeps the http options deeply unchanged through YAML text', async () => {
    const schema = await withSourceFields({
      type: 'http_json',
      http: HTTP_OPTIONS,
      extra: { future_option: 'kept' },
    });
    const text = dumpSchemaYaml(schemaToDocument(schema));
    const parsed = parseSchemaYaml(text);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const { schema: back, issues } = documentToSchema(parsed.document, schema, mint);
    expect(issues).toEqual([]);
    expect(back.sources[0]?.http).toEqual(HTTP_OPTIONS);
    expect(back.sources).toEqual(schema.sources);
  });

  it('never lets an extra key override a modelled one', async () => {
    const schema = await withSourceFields({ extra: { type: 'csv', connection: 'literal' } });
    const source = schemaToDocument(schema).sources?.[schema.sources[0]?.api_name ?? ''];
    expect(source?.['type']).toBe(schema.sources[0]?.type);
    expect(source?.['connection']).toBe(schema.sources[0]?.connection);
  });
});

describe('sections the Modeler does not edit', () => {
  async function enriched(): Promise<Schema> {
    const schema = await sample('main');
    expect(schema.lenses?.length).toBeGreaterThan(0);
    return {
      ...schema,
      domain_info: { stewards: ['supply-steward'], extra: { future_domain_key: 1 } },
      kinds: schema.kinds.map((k) =>
        k.api_name === 'Supplier'
          ? { ...k, stewards: ['risk-analyst'], extra: { future_kind_key: { a: [1] } } }
          : k,
      ),
      lenses: (schema.lenses ?? []).map((l, i) =>
        i === 0 ? { ...l, extra: { depth: 1, budget_tokens: 2000 } } : l,
      ),
      extra: { retention: { default_days: 90 } },
    };
  }

  it('writes lenses, stewards and unknown members into the document', async () => {
    const schema = await enriched();
    const document = schemaToDocument(schema);
    const lens = schema.lenses?.[0];
    expect(Object.keys(document.lenses ?? {})).toEqual(schema.lenses?.map((l) => l.api_name));
    expect(document.lenses?.[lens?.api_name ?? '']).toMatchObject({
      id: lens?.id,
      depth: 1,
      budget_tokens: 2000,
    });
    expect(document.domain).toMatchObject({ stewards: ['supply-steward'], future_domain_key: 1 });
    expect(document.kinds?.['Supplier']).toMatchObject({
      stewards: ['risk-analyst'],
      future_kind_key: { a: [1] },
    });
    expect((document as Record<string, unknown>)['retention']).toEqual({ default_days: 90 });
  });

  it('keeps them through an edit of an unrelated Kind and a save', async () => {
    const schema = await enriched();
    const edited = structuredClone(schemaToDocument(schema));
    const plant = edited.kinds?.['Plant'];
    if (plant === undefined) throw new Error('No Plant');
    plant.description = 'Edited';
    const { schema: back, issues } = documentToSchema(edited, schema, mint);
    expect(issues).toEqual([]);
    expect(back.lenses).toEqual(schema.lenses);
    expect(back.extra).toEqual(schema.extra);
    expect(back.kinds.find((k) => k.api_name === 'Supplier')).toEqual(
      schema.kinds.find((k) => k.api_name === 'Supplier'),
    );
    expect(schemaToDocument(back)).toEqual(edited);
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

describe('members the Modeler does not model, inside nested objects', () => {
  async function nested(): Promise<Schema> {
    const schema = structuredClone(await sample('model/risk'));
    const kind = schema.kinds.find((k) => k.traits.length > 0);
    const operation = schema.operations.find(
      (o) => !o.builtin && o.parameters.length > 0 && o.preconditions.length > 0,
    );
    const source = schema.sources[0];
    const relation = schema.relations[0];
    const implementation = kind?.traits[0];
    const parameter = operation?.parameters[0];
    const precondition = operation?.preconditions[0];
    if (!kind || !source || !relation || !implementation || !parameter || !precondition) {
      throw new Error('The sample lacks an element this test needs');
    }
    kind.search = { ...kind.search, embedding_template: 'x', extra: { future_search: { a: [1] } } };
    implementation.extra = { future_link: 'x' };
    const attribute = Object.values(kind.attributes)[0];
    if (!attribute) throw new Error('No attribute');
    Object.assign(attribute.constraints, { future_constraint: 7 });
    parameter.extra = { future_parameter: [1, 2] };
    precondition.extra = { future_precondition: { b: 1 } };
    source.relation_mapping = [
      { relation: relation.id, columns: ['c'], extra: { future_mapping: 'm' } },
    ];
    source.target_branch_policy = {
      ...source.target_branch_policy,
      extra: { future_policy: { c: true } },
    };
    return schema;
  }

  it('writes them into the document', async () => {
    const json = JSON.stringify(schemaToDocument(await nested()));
    for (const key of [
      'future_search',
      'future_link',
      'future_constraint',
      'future_parameter',
      'future_precondition',
      'future_mapping',
      'future_policy',
    ]) {
      expect(json).toContain(`"${key}"`);
    }
    expect(json).not.toContain('"extra"');
  });

  it('keeps them through an edit of an unrelated Kind and a save', async () => {
    const schema = await nested();
    const edited = structuredClone(schemaToDocument(schema));
    const plant = edited.kinds?.['Plant'];
    if (plant === undefined) throw new Error('No Plant');
    plant.description = 'Edited';
    const { schema: back, issues } = documentToSchema(edited, schema, mint);
    expect(issues).toEqual([]);
    expect(back.relations).toEqual(schema.relations);
    expect(back.sources).toEqual(schema.sources);
    expect(custom(back)).toEqual(custom(schema));
    expect(back.kinds.filter((k) => k.api_name !== 'Plant')).toEqual(
      schema.kinds.filter((k) => k.api_name !== 'Plant'),
    );
    expect(schemaToDocument(back)).toEqual(edited);
  });
});
