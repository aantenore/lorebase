import type { InferenceEvidence } from '@/api/inference-types';
import type { SchemaDocument } from '@/features/modeler/document';
import {
  acceptAtOrAbove,
  analyze,
  countDecisions,
  decide,
  decideKind,
  enumSeed,
  inclusionRatios,
  indexEvidence,
  mergeIntoBase,
  nameIsFree,
  sharedNames,
  type Decisions,
} from './proposal-model';

const proposal: SchemaDocument = {
  kinds: {
    Supplier: {
      id: 'k1',
      primary_key: ['code'],
      title_attribute: 'name',
      attributes: {
        code: { id: 'a1', type: 'string' },
        name: { id: 'a2', type: 'string' },
        tier: { id: 'a3', type: 'integer' },
      },
    },
    Order: {
      id: 'k2',
      primary_key: ['number'],
      attributes: {
        number: { id: 'a4', type: 'string' },
        supplier: { id: 'a5', type: 'ref(Supplier)' },
      },
    },
  },
  relations: {
    supplier: {
      id: 'r1',
      inverse_api_name: 'orders',
      from_kind: 'Order',
      to_kind: 'Supplier',
      foreign_key_attribute: 'supplier',
    },
    supplied_by: {
      id: 'r2',
      inverse_api_name: 'parts',
      from_kind: 'Order',
      to_kind: 'Supplier',
      attributes: { lead_time: { id: 'a6', type: 'integer' } },
    },
  },
};

const evidence = (id: string, confidence: number): InferenceEvidence => ({
  element: id,
  element_id: id,
  source: 't',
  confidence,
  reasons: [],
  samples: ['x', 'y', 'x'],
});

const all = (value: 'accepted' | 'dropped'): Decisions =>
  Object.fromEntries(
    ['k1', 'k2', 'a1', 'a2', 'a3', 'a4', 'a5', 'r1', 'r2', 'a6'].map((id) => [id, value]),
  );

describe('decisions', () => {
  it('sets, replaces and clears one decision', () => {
    const first = decide({}, 'a1', 'accepted');
    expect(decide(first, 'a1', 'dropped')).toEqual({ a1: 'dropped' });
    expect(decide(first, 'a1', null)).toEqual({});
  });

  it('accepting a Kind accepts its undecided Attributes and keeps the dropped ones', () => {
    const kind = proposal.kinds?.['Supplier'];
    if (kind === undefined) throw new Error('fixture');
    const next = decideKind({ a3: 'dropped' }, kind, 'accepted');
    expect(next).toEqual({ k1: 'accepted', a1: 'accepted', a2: 'accepted', a3: 'dropped' });
    expect(decideKind(next, kind, 'dropped')['k1']).toBe('dropped');
  });

  it('counts accepted, dropped and pending elements', () => {
    expect(countDecisions(proposal, { k1: 'accepted', a1: 'dropped' })).toEqual({
      total: 10,
      accepted: 1,
      dropped: 1,
      pending: 8,
    });
  });
});

describe('acceptAtOrAbove', () => {
  const index = indexEvidence([evidence('k1', 1), evidence('a1', 0.9), evidence('a2', 0.89)]);

  it('accepts what reaches the threshold, edge included, and nothing without evidence', () => {
    const next = acceptAtOrAbove(proposal, index, {}, 0.9);
    expect(next).toEqual({ k1: 'accepted', a1: 'accepted' });
  });

  it('never overrides a drop', () => {
    expect(acceptAtOrAbove(proposal, index, { k1: 'dropped' }, 0.5)['k1']).toBe('dropped');
  });
});

describe('analyze', () => {
  it('applies the accepted elements only', () => {
    const result = analyze(proposal, { k1: 'accepted', a2: 'accepted' });
    expect(Object.keys(result.document.kinds ?? {})).toEqual(['Supplier']);
    // The key comes with the Kind; tier was not accepted.
    expect(Object.keys(result.document.kinds?.['Supplier']?.attributes ?? {})).toEqual([
      'code',
      'name',
    ]);
    expect(result.applied).toEqual({ kinds: 1, attributes: 2, relations: 0 });
  });

  it('leaves out an Attribute that references a Kind that is not accepted', () => {
    const result = analyze(proposal, { k2: 'accepted', a5: 'accepted' });
    expect(result.document.kinds?.['Order']?.attributes).not.toHaveProperty('supplier');
    expect(result.notApplied.get('a5')).toBe('Supplier is not accepted');
  });

  it('leaves out a Relation when an end is missing or its reference Attribute is left out', () => {
    const noEnd = analyze(proposal, { k2: 'accepted', r1: 'accepted' });
    expect(noEnd.notApplied.get('r1')).toBe('Supplier is not accepted');

    const noReference = analyze(proposal, { k1: 'accepted', k2: 'accepted', r1: 'accepted' });
    expect(noReference.notApplied.get('r1')).toMatch(/reference Attribute supplier is left out/);

    const complete = analyze(proposal, {
      k1: 'accepted',
      k2: 'accepted',
      a5: 'accepted',
      r1: 'accepted',
    });
    expect(complete.applied.relations).toBe(1);
  });

  it('keeps only the accepted link Attributes of a Relation', () => {
    const none = analyze(proposal, { k1: 'accepted', k2: 'accepted', r2: 'accepted' });
    expect(none.document.relations?.['supplied_by']?.attributes).toEqual({});
    const some = analyze(proposal, { ...all('accepted') });
    expect(Object.keys(some.document.relations?.['supplied_by']?.attributes ?? {})).toEqual([
      'lead_time',
    ]);
  });

  it('picks another title when the title Attribute is not applied', () => {
    const dropped = analyze(proposal, { k1: 'accepted' });
    expect(dropped.document.kinds?.['Supplier']?.title_attribute).toBe('code');
  });
});

describe('mergeIntoBase', () => {
  const base: SchemaDocument = {
    kinds: {
      Supplier: {
        id: 'base-k',
        primary_key: ['supplier_id'],
        title_attribute: 'legal_name',
        attributes: {
          supplier_id: { id: 'base-a0', type: 'string' },
          name: { id: 'base-a2', type: 'text' },
        },
      },
    },
    relations: { supplier: { id: 'base-r', inverse_api_name: 'x', from_kind: 'A', to_kind: 'B' } },
  };

  it('adds new Kinds and Relations as they are', () => {
    const merged = mergeIntoBase({}, analyze(proposal, all('accepted')).document);
    expect(Object.keys(merged.kinds ?? {})).toEqual(['Supplier', 'Order']);
    expect(merged.kinds?.['Order']?.id).toBe('k2');
  });

  it('merges into a Kind that exists: its key and id stay, the Attribute keeps its id', () => {
    const merged = mergeIntoBase(base, analyze(proposal, all('accepted')).document);
    const supplier = merged.kinds?.['Supplier'];
    expect(supplier?.id).toBe('base-k');
    expect(supplier?.primary_key).toEqual(['supplier_id']);
    expect(supplier?.attributes?.['name']).toMatchObject({ id: 'base-a2', type: 'string' });
    expect(supplier?.attributes?.['tier']?.id).toBe('a3');
    expect(supplier?.attributes?.['supplier_id']?.id).toBe('base-a0');
  });

  it('keeps a Relation that exists as it is', () => {
    const merged = mergeIntoBase(base, analyze(proposal, all('accepted')).document);
    expect(merged.relations?.['supplier']?.id).toBe('base-r');
    expect(merged.relations?.['supplied_by']?.id).toBe('r2');
  });

  it('names what the proposal shares with the Branch', () => {
    const shared = sharedNames(base, proposal);
    expect([...shared.kinds]).toEqual(['Supplier']);
    expect([...shared.relations]).toEqual(['supplier']);
  });
});

describe('small helpers', () => {
  it('reads inclusion ratios from the reasons of a reference', () => {
    expect(
      inclusionRatios([
        'PurchaseOrder.supplier references Supplier',
        '100% of 22 distinct values are keys of supplier(supplier_code)',
        '96.5% of 40 distinct values are keys of part(part_number)',
      ]),
    ).toEqual([1, 0.965]);
    expect(inclusionRatios(['no match'])).toEqual([]);
  });

  it('checks that a name is free among siblings', () => {
    expect(nameIsFree(['a', 'b'], 'a', 'a')).toBe(true);
    expect(nameIsFree(['a', 'b'], 'a', 'b')).toBe(false);
    expect(nameIsFree(['a', 'b'], 'a', '')).toBe(false);
    expect(nameIsFree(['a', 'b'], 'a', 'c')).toBe(true);
  });

  it('seeds enum values from the distinct samples', () => {
    expect(enumSeed(evidence('x', 1))).toEqual(['x', 'y']);
    expect(enumSeed(undefined)).toEqual([]);
  });
});
