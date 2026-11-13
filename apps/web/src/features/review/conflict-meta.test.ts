import type { AutoResolution, Conflict } from '@/api/types';
import {
  bulkChoiceLabel,
  locationOf,
  messageOf,
  partitionName,
  policyReason,
  sideChange,
  specOf,
} from './conflict-meta';
import { makeConflict } from '@/api/mock/conflicts';
import { mapSchema } from '@/api/live/schema';
import fixture from '@/api/live/fixtures/supply-schema.json';

const schema = mapSchema(fixture as Parameters<typeof mapSchema>[0]);
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

describe('bulkChoiceLabel', () => {
  const dropOn = (kind: 'XS1' | 'XS9') => makeConflict({ kind, partition: 'p', message: 'm' });

  it('uses the label the Conflicts share', () => {
    expect(bulkChoiceLabel([dropOn('XS9')], 'drop')).toBe('Drop the Links');
    expect(bulkChoiceLabel([conflict], 'ours')).toBe('Take ours');
  });

  it('falls back to a general name when the kinds word the choice differently', () => {
    expect(bulkChoiceLabel([dropOn('XS9'), dropOn('XS1')], 'drop')).toBe('Drop');
  });
});

describe('sideChange', () => {
  const provenance = {
    ours: {
      provenance_id: 'prv_erp',
      at: '2026-03-01T10:00:00Z',
      actor: 'scribe',
      origin: 'source_sync',
      source: 'src_erp',
      principal: null,
      labels: ['actor:scribe', 'origin:source_sync', 'source:src_erp'],
    },
    theirs: {
      provenance_id: 'prv_edit',
      at: '2026-03-02T10:00:00Z',
      actor: 'usr_analyst',
      origin: 'operation',
      principal: 'usr_analyst',
    },
  };

  it('names the Source of a synced side and the principal of an edited one', () => {
    expect(sideChange({ provenance }, 'ours')).toEqual({
      who: 'source src_erp',
      origin: 'source_sync',
      at: '2026-03-01T10:00:00Z',
      provenanceId: 'prv_erp',
    });
    expect(sideChange({ provenance }, 'theirs')).toMatchObject({
      who: 'usr_analyst',
      origin: 'operation',
      provenanceId: 'prv_edit',
    });
  });

  it('falls back to the actor, and is null without provenance for the side', () => {
    const actorOnly = { ours: { provenance_id: 'prv_1', at: '2026-03-01T10:00:00Z', actor: 'a' } };
    expect(sideChange({ provenance: actorOnly }, 'ours')?.who).toBe('a');
    expect(sideChange({ provenance: actorOnly }, 'theirs')).toBeNull();
    expect(sideChange({ provenance: null }, 'ours')).toBeNull();
    expect(sideChange(conflict, 'ours')).toBeNull();
  });
});

describe('policyReason', () => {
  const auto: AutoResolution = {
    kind: 'DV1',
    fingerprint: 'f',
    partition: 'k:knd_x',
    key: 'S-1',
    attribute: 'att_x',
    choice: 'theirs',
    resolver: 'policy:authority',
    count: 1,
    sample: [],
  };

  it('names the winning and losing origins of authority', () => {
    const details = { theirs_origin: 'source:src_erp', ours_origin: null };
    expect(policyReason({ ...auto, details })).toBe('source:src_erp outranks an unranked origin');
  });

  it('names the compared times of newest_wins', () => {
    const details = {
      ours_at: '2026-03-02T10:00:00+00:00',
      theirs_at: '2026-03-01T10:00:00+00:00',
    };
    expect(policyReason({ ...auto, choice: 'ours', resolver: 'policy:newest_wins', details })).toBe(
      'changed 2026-03-02T10:00:00+00:00, after 2026-03-01T10:00:00+00:00',
    );
    expect(
      policyReason({
        ...auto,
        resolver: 'policy:newest_wins',
        details: { ...details, tie_break: 'provenance_id' },
      }),
    ).toBe('same time 2026-03-01T10:00:00+00:00, greater provenance id');
  });

  it('counts the changes text_merge took from each side', () => {
    const details = { ours_hunks: 1, theirs_hunks: 2 };
    expect(
      policyReason({ ...auto, choice: 'custom', resolver: 'policy:text_merge', details }),
    ).toBe('text merged: 1 change from ours, 2 from theirs');
  });

  it('has no reason for prefer', () => {
    expect(policyReason({ ...auto, resolver: 'policy:prefer' })).toBeNull();
  });
});
