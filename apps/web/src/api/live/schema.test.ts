// @vitest-environment node
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';
import { describe, expect, it } from 'vitest';
import { documentToSchema, schemaToDocument } from '@/features/modeler/document';
import { mapSchema } from './schema';
import type { components } from '../generated/keeper';

const fixture = (name: string) =>
  readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), 'utf8');

const out = JSON.parse(fixture('supply-schema.json')) as components['schemas']['SchemaOut'];
const realDocument = parse(fixture('supply-schema.yaml')) as Record<string, unknown>;

type Doc = Record<string, unknown>;

/** Values Keeper fills in when a document leaves them out. */
const DEFAULTS: Record<string, unknown> = {
  display_name: '',
  cardinality: 'many_to_many',
  direction: 'forward',
  sync_mode: 'snapshot',
  branch_template: 'source/{source}',
};

/** Drop what a document may spell either way: defaults and empty containers. */
function normalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalize);
  if (typeof value !== 'object' || value === null) return value;
  const entries = Object.entries(value as Doc)
    .filter(([key, v]) => !(key in DEFAULTS && DEFAULTS[key] === v))
    .map(([key, v]) => [key, normalize(v)] as const)
    .filter(([, v]) => !(typeof v === 'object' && v !== null && Object.keys(v).length === 0));
  return Object.fromEntries(entries);
}

describe('mapSchema', () => {
  const schema = mapSchema(out);

  it('maps every section of the supply schema', () => {
    expect(schema.kinds.map((k) => k.api_name).sort()).toEqual([
      'Contract',
      'Part',
      'Plant',
      'PurchaseOrder',
      'RiskFlag',
      'Supplier',
    ]);
    expect(schema.relations).toHaveLength(7);
    expect(schema.traits.map((t) => t.api_name)).toEqual(['Located']);
    expect(schema.derivations).toHaveLength(1);
    expect(schema.sources).toHaveLength(2);
  });

  it('leaves out Relations reaching a Kind the schema does not list', () => {
    const body = out.schema as Record<string, unknown>;
    const kinds = body['kinds'] as Record<string, Record<string, unknown>>;
    const kept = Object.fromEntries(
      Object.entries(kinds).filter(([, kind]) => kind['api_name'] !== 'Supplier'),
    );
    const limited = mapSchema({ ...out, schema: { ...body, kinds: kept } });
    const ids = new Set(limited.kinds.map((k) => k.id));
    expect(limited.kinds.map((k) => k.api_name)).not.toContain('Supplier');
    expect(limited.relations.length).toBeLessThan(schema.relations.length);
    for (const relation of limited.relations) {
      expect(ids.has(relation.from_kind) && ids.has(relation.to_kind)).toBe(true);
    }
  });

  it('spells types the way the screens read them', () => {
    const part = schema.kinds.find((k) => k.api_name === 'Part');
    const byName = Object.fromEntries(
      Object.values(part?.attributes ?? {}).map((a) => [a.api_name, a]),
    );
    expect(byName['unit_cost']?.type).toBe('decimal(10,2)');
    expect(byName['category']?.type).toBe('enum');
    expect(byName['category']?.constraints.enum_values).toContain('bearing');
    expect(byName['name']?.searchable).toBe('A');
    const order = schema.kinds.find((k) => k.api_name === 'PurchaseOrder');
    const supplier = Object.values(order?.attributes ?? {}).find((a) => a.api_name === 'supplier');
    expect(supplier?.type).toBe('ref(Supplier)');
  });

  it('writes the document Keeper serves, so saving an untouched schema changes nothing', () => {
    const document = schemaToDocument(schema);
    expect(normalize(document)).toEqual(normalize(realDocument));
  });

  it('reads the document back into the same schema', () => {
    const document = schemaToDocument(schema);
    const back = documentToSchema(document, schema, () => {
      throw new Error('no element should need a new id');
    });
    expect(back.issues).toEqual([]);
    expect(back.schema.kinds).toEqual(schema.kinds);
    expect(back.schema.relations).toEqual(schema.relations);
    expect(back.schema.derivations).toEqual(schema.derivations);
    expect(back.schema.sources).toEqual(schema.sources);
  });

  it('keeps the fields of file and HTTP Sources and the keys it does not model', () => {
    const http = {
      headers: { Authorization: 'secret://env/API_TOKEN' },
      pagination: { type: 'page', page_param: 'p' },
      max_pages: 5,
    };
    const copy = structuredClone(out);
    const sources = copy.schema['sources'] as Record<string, Record<string, unknown>>;
    const [id] = Object.keys(sources);
    if (id === undefined) throw new Error('No Source');
    Object.assign(sources[id] as Record<string, unknown>, {
      type: 'http_json',
      table: 'erp.vendors',
      query: 'SELECT 1',
      records_path: 'data.items',
      flatten_separator: '__',
      http,
      future_option: { nested: [1, 2] },
    });
    const mapped = mapSchema(copy).sources.find((s) => s.id === id);
    expect(mapped).toMatchObject({
      type: 'http_json',
      table: 'erp.vendors',
      query: 'SELECT 1',
      records_path: 'data.items',
      flatten_separator: '__',
      http,
      extra: { future_option: { nested: [1, 2] } },
    });
    expect(Object.keys(mapped?.extra ?? {})).toEqual(['future_option']);
  });

  it('leaves out the optional Source fields and extra when Keeper sends none', () => {
    for (const source of schema.sources) {
      expect(source).not.toHaveProperty('extra');
      expect(source).not.toHaveProperty('http');
      expect(source).not.toHaveProperty('records_path');
    }
  });
});

describe('sections the Modeler does not edit', () => {
  type Obj = Record<string, unknown>;

  /** The mapping at `path` below `value`. */
  const at = (value: unknown, ...path: string[]): Obj =>
    path.reduce<unknown>((node, key) => (node as Obj)[key], value) as Obj;
  const firstOf = (value: unknown): Obj => Object.values(value as Obj)[0] as Obj;

  /** Keeper's `SchemaOut` with lenses, stewards and members the view does not model. */
  function enriched() {
    const copy = structuredClone(out);
    const body = copy.schema as Obj;
    const byName = (section: string, name: string) => {
      const found = Object.values(at(body, section)).find((e) => (e as Obj)['api_name'] === name);
      if (found === undefined) throw new Error(`No ${name}`);
      return found as Obj;
    };
    const supplier = byName('kinds', 'Supplier');
    const supplied = byName('relations', 'supplied_by');
    at(body, 'domain')['stewards'] = ['supply-steward'];
    at(body, 'domain')['future_domain_key'] = { a: 1 };
    supplier['stewards'] = ['risk-analyst'];
    supplier['future_kind_key'] = ['x'];
    supplied['future_relation_key'] = true;
    const located = byName('traits', 'Located');
    located['future_trait_key'] = { b: 2 };
    firstOf(located['attributes'])['future_trait_attribute_key'] = 'y';
    firstOf(supplier['attributes'])['future_attribute_key'] = 3;
    firstOf(body['derivations'])['future_derivation_key'] = 'z';
    const lensId = 'lns_01KEZF7C00LENS000000000001';
    body['lenses'] = {
      [lensId]: {
        id: lensId,
        api_name: 'supplier_risk',
        description: 'A supplier with its parts and open risk flags.',
        kinds: [supplier['id']],
        traits: [],
        relations: [supplied['id']],
        depth: 1,
        include_recent_changes: true,
        budget_tokens: 4000,
      },
    };
    body['retention'] = {
      default_days: 90,
      rules: [{ kind: 'Part', days: 30 }],
    };
    return copy;
  }

  const noNewId = () => {
    throw new Error('no element should need a new id');
  };
  const document = () => schemaToDocument(mapSchema(enriched()));

  it('writes lenses, stewards and unknown members into the document verbatim', () => {
    const doc = document();
    expect(doc.domain).toMatchObject({
      stewards: ['supply-steward'],
      future_domain_key: { a: 1 },
    });
    expect(doc.domain).not.toHaveProperty('default_branch');
    expect(at(doc, 'kinds', 'Supplier')).toMatchObject({
      stewards: ['risk-analyst'],
      future_kind_key: ['x'],
    });
    expect(at(doc, 'relations', 'supplied_by')).toMatchObject({ future_relation_key: true });
    expect(at(doc, 'traits', 'Located')).toMatchObject({ future_trait_key: { b: 2 } });
    expect(firstOf(at(doc, 'traits', 'Located', 'attributes'))).toMatchObject({
      future_trait_attribute_key: 'y',
    });
    expect(firstOf(at(doc, 'kinds', 'Supplier', 'attributes'))).toMatchObject({
      future_attribute_key: 3,
    });
    expect(firstOf(doc.derivations)).toMatchObject({ future_derivation_key: 'z' });
    expect(doc.lenses).toEqual({
      supplier_risk: {
        id: 'lns_01KEZF7C00LENS000000000001',
        description: 'A supplier with its parts and open risk flags.',
        kinds: ['Supplier'],
        relations: ['supplied_by'],
        depth: 1,
        include_recent_changes: true,
        budget_tokens: 4000,
      },
    });
    expect(at(doc, 'retention')).toEqual({ default_days: 90, rules: [{ kind: 'Part', days: 30 }] });
  });

  it('keeps every untouched part when an unrelated Kind is edited and the document saved', () => {
    const schema = mapSchema(enriched());
    const edited = document();
    at(edited, 'kinds', 'Plant')['description'] = 'Edited';
    const back = documentToSchema(edited, schema, noNewId);
    expect(back.issues).toEqual([]);
    expect(back.schema.lenses).toEqual(schema.lenses);
    expect(back.schema.extra).toEqual(schema.extra);
    expect(schemaToDocument(back.schema)).toEqual(edited);
  });

  it('reads an unedited document back into the schema it came from', () => {
    const schema = mapSchema(enriched());
    const back = documentToSchema(document(), schema, noNewId);
    expect(back.issues).toEqual([]);
    expect(back.schema.kinds).toEqual(schema.kinds);
    expect(back.schema.relations).toEqual(schema.relations);
    expect(back.schema.traits).toEqual(schema.traits);
    expect(back.schema.derivations).toEqual(schema.derivations);
    expect(back.schema.lenses).toEqual(schema.lenses);
    expect(back.schema.extra).toEqual({
      retention: { default_days: 90, rules: [{ kind: 'Part', days: 30 }] },
    });
  });
});
