import type { SchemaDocument } from './document';
import {
  addKind,
  enumValuesOf,
  renameAttribute,
  renameKind,
  typeFromLabel,
  typeLabel,
} from './form-helpers';

const ids = (prefix: string, key: string) => `${prefix}_${key}`;

const document = (): SchemaDocument => ({
  kinds: {
    Part: {
      primary_key: ['part_number'],
      title_attribute: 'part_number',
      attributes: { part_number: { type: 'string' }, supplier: { type: 'ref(Supplier)' } },
      traits: [{ trait: 'Located', attribute_map: { country: 'origin' } }],
    },
    Supplier: { attributes: { code: { type: 'string' } } },
  },
  relations: {
    supplied_by: {
      inverse_api_name: 'supplies',
      from_kind: 'Part',
      to_kind: 'Supplier',
      foreign_key_attribute: 'supplier',
    },
  },
  derivations: { d: { target: { type: 'attribute', kind: 'Part', name: 'x' } } },
  sources: { s: { target_kind: 'Part' } },
});

describe('form helpers', () => {
  it('renames a Kind everywhere the document names it', () => {
    const draft = document();
    renameKind(draft, 'Supplier', 'Vendor');
    expect(Object.keys(draft.kinds ?? {})).toEqual(['Part', 'Vendor']);
    expect(draft.relations?.['supplied_by']?.to_kind).toBe('Vendor');
    expect(draft.kinds?.['Part']?.attributes?.['supplier']?.type).toBe('ref(Vendor)');

    renameKind(draft, 'Part', 'Component');
    expect(draft.relations?.['supplied_by']?.from_kind).toBe('Component');
    expect(draft.derivations?.['d']?.['target']).toMatchObject({ kind: 'Component' });
    expect(draft.sources?.['s']?.['target_kind']).toBe('Component');
  });

  it('renames an Attribute inside its Kind, keeping order and references', () => {
    const draft = document();
    renameAttribute(draft, 'Part', 'part_number', 'number');
    const part = draft.kinds?.['Part'];
    expect(Object.keys(part?.attributes ?? {})).toEqual(['number', 'supplier']);
    expect(part?.primary_key).toEqual(['number']);
    expect(part?.title_attribute).toBe('number');

    renameAttribute(draft, 'Part', 'supplier', 'vendor');
    expect(draft.relations?.['supplied_by']?.foreign_key_attribute).toBe('vendor');
    if (part?.attributes === undefined) throw new Error('No Part attributes');
    part.attributes['origin'] = { type: 'string' };
    renameAttribute(draft, 'Part', 'origin', 'country_of_origin');
    expect(part.traits?.[0]?.attribute_map).toEqual({ country: 'country_of_origin' });
  });

  it('starts a new Kind with a required name key', () => {
    const draft = document();
    addKind(draft, 'Warehouse', ids);
    expect(draft.kinds?.['Warehouse']).toMatchObject({
      id: 'knd_kinds/Warehouse',
      primary_key: ['name'],
      title_attribute: 'name',
    });
  });

  it('maps a type to its select value and back', () => {
    expect(typeLabel({ enum: ['a'] })).toBe('enum');
    expect(typeLabel({ array: { enum: ['a'] } })).toBe('array<enum>');
    expect(typeLabel({ array: 'string' })).toBe('array<string>');
    expect(typeFromLabel('array<integer>', [])).toEqual({ array: 'integer' });
    expect(typeFromLabel('enum', ['x', 'y'])).toEqual({ enum: ['x', 'y'] });
    expect(enumValuesOf({ array: { enum: ['x'] } })).toEqual(['x']);
  });
});
