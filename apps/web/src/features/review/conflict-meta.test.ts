import type { Conflict } from '@/api/types';
import { locationOf, messageOf, partitionName, specOf } from './conflict-meta';
import { makeConflict } from '@/api/mock/conflicts';
import { mapSchema } from '@/api/live/schema';
import fixture from '@/api/live/fixtures/supply-schema.json';

const schema = mapSchema(fixture);
const supplier = schema.kinds.find((k) => k.api_name === 'Supplier');
const risk = Object.values(supplier?.attributes ?? {}).find((a) => a.api_name === 'risk_score');
if (supplier === undefined || risk === undefined) throw new Error('fixture lost Supplier');

const names = new Map([
  [supplier.id, 'Supplier'],
  [risk.id, 'risk_score'],
]);

/** A Conflict the way Keeper reports it: the partition and the Attribute are ids. */
const conflict: Conflict = makeConflict({
  kind: 'DV1',
  partition: `k:${supplier.id}`,
  key: 'S-1001',
  attribute: risk.id,
  base: '32',
  ours: '80',
  theirs: '45',
  message: `${risk.id} of S-1001 changed differently on both sides`,
});

describe('Conflicts as Keeper reports them', () => {
  it('names a data partition by its Kind or Relation, and leaves other names alone', () => {
    expect(partitionName(`k:${supplier.id}`, names)).toBe('Supplier');
    expect(partitionName('r:rel_unknown', names)).toBe('rel_unknown');
    expect(partitionName('Supplier', names)).toBe('Supplier');
    expect(partitionName('schema', names)).toBe('schema');
  });

  it('locates a Conflict by Kind, key and Attribute', () => {
    expect(locationOf(conflict, names)).toBe('Supplier/S-1001 · risk_score');
  });

  it('replaces the element ids in the message with names', () => {
    expect(messageOf(conflict, names)).toBe(
      'risk_score of S-1001 changed differently on both sides',
    );
  });

  it('finds the type of a custom value through the partition id', () => {
    expect(specOf(conflict, schema)).toMatchObject({
      type: 'integer',
      constraints: { min: 0, max: 100 },
    });
  });
});
