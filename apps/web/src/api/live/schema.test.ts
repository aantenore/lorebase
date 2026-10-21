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
});
