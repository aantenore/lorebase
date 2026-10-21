import type { ExtractionItem } from '@/api/extraction-types';
import {
  boundOf,
  editsFor,
  effective,
  groupByType,
  neighbor,
  passes,
  stage,
  stageEdits,
  tableOrder,
  toDecideInput,
  toFilter,
  NO_ITEM_FILTER,
  type Draft,
} from './item-state';

function item(id: string, over: Partial<ExtractionItem> = {}): ExtractionItem {
  return {
    id,
    type: 'value',
    seq: Number(id.slice(1)),
    decision: 'pending',
    confidence: 0.5,
    tier: 0,
    model: null,
    kind: 'Supplier',
    relation: null,
    key: 'S-1',
    attribute: 'risk_score',
    value: '10',
    current: '5',
    from: null,
    to: null,
    values: {},
    evidence: null,
    match: null,
    edits: null,
    decided_by: null,
    decided_at: null,
    not_applied: null,
    ...over,
  };
}

describe('item filters', () => {
  it('reads a bound from 0 to 1.01 and nothing else', () => {
    expect(boundOf('')).toBeUndefined();
    expect(boundOf('abc')).toBeUndefined();
    expect(boundOf('-0.1')).toBeUndefined();
    expect(boundOf('1.5')).toBeUndefined();
    expect(boundOf('0.8')).toBe(0.8);
    expect(boundOf('1.01')).toBe(1.01);
  });

  it('keeps the lower bound inclusive and the upper bound exclusive, like Keeper', () => {
    const filter = toFilter({ ...NO_ITEM_FILTER, min: '0.6', max: '0.8' });
    expect(filter).toEqual({ min_confidence: 0.6, max_confidence: 0.8 });
    expect(passes(item('i1', { confidence: 0.6 }), filter)).toBe(true);
    expect(passes(item('i2', { confidence: 0.79 }), filter)).toBe(true);
    expect(passes(item('i3', { confidence: 0.8 }), filter)).toBe(false);
    expect(passes(item('i4', { confidence: 0.59 }), filter)).toBe(false);
  });

  it('matches the Kind of a value and the Relation of a link', () => {
    const link = item('i5', { type: 'link', kind: null, relation: 'feeds' });
    expect(passes(link, { kind: ['feeds'] })).toBe(true);
    expect(passes(link, { kind: ['Supplier'] })).toBe(false);
    expect(passes(item('i6'), { kind: ['Supplier'], type: ['value'], status: ['pending'] })).toBe(
      true,
    );
    expect(passes(item('i6'), { status: ['accepted'] })).toBe(false);
  });
});

describe('item order', () => {
  it('groups by type in proposal order and walks the groups with wrapping', () => {
    const items = [
      item('i1', { type: 'link' }),
      item('i2', { type: 'record' }),
      item('i3', { type: 'value' }),
      item('i4', { type: 'record' }),
    ];
    expect(groupByType(items).map((g) => [g.type, g.items.map((i) => i.id)])).toEqual([
      ['record', ['i2', 'i4']],
      ['value', ['i3']],
      ['link', ['i1']],
    ]);
    const order = tableOrder(items);
    expect(neighbor(order, 'i1', 1)?.id).toBe('i2');
    expect(neighbor(order, 'i2', -1)?.id).toBe('i1');
    expect(neighbor(order, null, 1)?.id).toBe('i2');
    expect(neighbor(order, null, -1)?.id).toBe('i1');
    expect(neighbor([], null, 1)).toBeUndefined();
  });
});

describe('staged decisions', () => {
  const items = [item('i1'), item('i2', { decision: 'accepted' })];

  it('stages a change and forgets a decision that restores what Keeper holds', () => {
    const accepted = stage(new Map(), items, 'accepted');
    expect([...accepted.keys()]).toEqual(['i1']);
    const reset = stage(accepted, items, 'pending');
    expect([...reset.keys()]).toEqual(['i2']);
    expect(stage(reset, [item('i2', { decision: 'accepted' })], 'accepted').size).toBe(0);
  });

  it('lays the staged decision and edits over the item', () => {
    const draft: Draft = new Map([['i1', { decision: 'accepted', edits: { value: '99' } }]]);
    expect(effective(items[0] as ExtractionItem, draft)).toMatchObject({
      decision: 'accepted',
      value: '99',
    });
    expect(effective(items[1] as ExtractionItem, draft)).toBe(items[1]);
  });

  it('keeps only edits that differ, and merges them with the staged ones', () => {
    const record = item('i3', { type: 'record', values: { title: 'A', currency: 'EUR' } });
    const first = editsFor(record, { values: { currency: 'PLN', title: 'A' } });
    expect(first).toEqual({ values: { currency: 'PLN' } });
    const second = editsFor(record, { values: { title: 'B' } }, first);
    expect(second).toEqual({ values: { currency: 'PLN', title: 'B' } });
    expect(editsFor(record, { values: { currency: 'EUR' } })).toBeUndefined();
    expect(editsFor(item('i4'), { value: '10' })).toBeUndefined();
    expect(editsFor(item('i4', { type: 'link', from: 'a', to: 'b' }), { to: 'c' })).toEqual({
      to: 'c',
    });
  });

  it('stages edits next to the decision and sends both once', () => {
    const base = item('i1');
    let draft = stage(new Map(), [base], 'accepted');
    draft = stageEdits(draft, base, { value: '12' });
    expect(toDecideInput(draft)).toEqual({
      decisions: [{ item: 'i1', decision: 'accepted', edits: { value: '12' } }],
    });
    expect(stageEdits(new Map(), base, undefined).size).toBe(0);
  });
});
