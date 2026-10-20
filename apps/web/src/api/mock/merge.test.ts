import type { KeeperApi } from '../client';
import type { Conflict, ConflictCode, MergeAttempt, Resolution } from '../types';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };
const supply = { domain: 'supply' };

const setup = (): KeeperApi => asRemote(createMockApi(options), 0);

const conflictOf = (attempt: MergeAttempt, kind: ConflictCode): Conflict => {
  const found = attempt.conflicts.find((c) => c.kind === kind);
  if (found === undefined) throw new Error(`No ${kind} Conflict in the attempt.`);
  return found;
};

/** A Resolution for every Conflict of Proposal 41, picking the given choice per kind. */
function fullResolutions(attempt: MergeAttempt): Resolution[] {
  const by = (kind: ConflictCode) => conflictOf(attempt, kind).fingerprint;
  return [
    { fingerprint: by('SC1'), choice: 'custom', value: 'Risk score from the risk model, 0 to 60.' },
    { fingerprint: by('DV1'), choice: 'theirs' },
    { fingerprint: by('DV2'), choice: 'ours' },
    { fingerprint: by('XS9'), choice: 'restore' },
    {
      fingerprint: by('XS4'),
      choice: 'custom',
      values: { 'S-1004': 'medium', 'S-1005': 'low', 'S-1006': 'high' },
    },
    { fingerprint: by('XS5'), choice: 'relax' },
  ];
}

describe('mock merge attempts', () => {
  it('seeds a Proposal with six Conflicts of different kinds', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');

    expect(attempt.conflicts.map((c) => c.kind).sort()).toEqual([
      'DV1',
      'DV2',
      'SC1',
      'XS4',
      'XS5',
      'XS9',
    ]);
    expect(attempt.resolutions).toEqual([]);
    expect(attempt.auto_resolutions).toHaveLength(1);
    expect(attempt.auto_resolutions[0]?.resolver).toBe('policy:erp-authority');

    const grouped = conflictOf(attempt, 'XS4');
    expect(grouped).toMatchObject({ count: 3, sample: ['S-1004', 'S-1005', 'S-1006'] });
    expect(grouped.choices).toEqual(['custom', 'relax']);
    expect(conflictOf(attempt, 'DV1').choices).toEqual(['ours', 'theirs', 'base', 'custom']);
    expect(conflictOf(attempt, 'DV2').theirs).toBeNull();
    expect(new Set(attempt.conflicts.map((c) => c.fingerprint)).size).toBe(6);
  });

  it('computes base, ours and theirs from the Branch heads', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const main = await api.branches.get('supply', 'main');
    const risk = await api.branches.get('supply', 'model/risk');
    expect(attempt).toMatchObject({ ours: main.head, theirs: risk.head, base: 'b83f60c1a5e9' });
  });

  it('refuses a choice the Conflict kind does not accept', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const fingerprint = conflictOf(attempt, 'XS4').fingerprint;
    await expect(
      api.conflicts.resolve('supply', '41', { fingerprint, choice: 'ours' }),
    ).rejects.toMatchObject({ status: 422, code: 'invalid_resolution' });
  });

  it('needs a value for custom, and for every member of a group', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const dv1 = conflictOf(attempt, 'DV1').fingerprint;
    const xs4 = conflictOf(attempt, 'XS4').fingerprint;
    await expect(
      api.conflicts.resolve('supply', '41', { fingerprint: dv1, choice: 'custom' }),
    ).rejects.toMatchObject({ code: 'invalid_resolution' });
    await expect(
      api.conflicts.resolve('supply', '41', { fingerprint: dv1, choice: 'theirs', value: '1' }),
    ).rejects.toMatchObject({ code: 'invalid_resolution' });
    await expect(
      api.conflicts.resolve('supply', '41', {
        fingerprint: xs4,
        choice: 'custom',
        values: { 'S-1004': 'low' },
      }),
    ).rejects.toThrow('No value for S-1005, S-1006.');
    await expect(
      api.conflicts.resolve('supply', '41', { fingerprint: 'nope', choice: 'ours' }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it('stores nothing from a bulk request that contains a bad entry', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const result = api.conflicts.resolveMany('supply', '41', {
      resolutions: [
        { fingerprint: conflictOf(attempt, 'DV1').fingerprint, choice: 'ours' },
        { fingerprint: conflictOf(attempt, 'XS9').fingerprint, choice: 'relax' },
      ],
    });
    await expect(result).rejects.toMatchObject({ code: 'invalid_resolution' });
    expect((await api.conflicts.list('supply', '41')).resolutions).toEqual([]);
  });

  it('replaces the Resolution of a Conflict when it is resolved again', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const fingerprint = conflictOf(attempt, 'DV1').fingerprint;
    await api.conflicts.resolve('supply', '41', { fingerprint, choice: 'ours' });
    await api.conflicts.resolve('supply', '41', { fingerprint, choice: 'base' });
    const stored = (await api.conflicts.list('supply', '41')).resolutions;
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ choice: 'base', resolver: 'usr_mock' });
  });

  it('reports the Conflicts that remain and leaves the target untouched', async () => {
    const api = setup();
    const before = await api.branches.get('supply', 'main');
    const attempt = await api.conflicts.list('supply', '41');
    const resolutions = fullResolutions(attempt).slice(0, 4);
    await api.conflicts.resolveMany('supply', '41', { resolutions });

    const outcome = await api.proposals.merge('supply', '41');
    expect(outcome.status).toBe('conflicts');
    if (outcome.status !== 'conflicts') return;
    expect(outcome.attempt.resolutions).toHaveLength(4);
    const open = outcome.attempt.conflicts.filter(
      (c) => !outcome.attempt.resolutions.some((r) => r.fingerprint === c.fingerprint),
    );
    expect(open.map((c) => c.kind).sort()).toEqual(['XS4', 'XS5']);
    expect((await api.branches.get('supply', 'main')).head).toBe(before.head);
    expect((await api.proposals.get('supply', '41')).status).toBe('open');
  });

  it('re-validates custom values and refuses the ones the merged schema rejects', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const tier = conflictOf(attempt, 'DV1').fingerprint;
    await api.conflicts.resolveMany('supply', '41', {
      resolutions: [
        ...fullResolutions(attempt).filter((r) => r.fingerprint !== tier),
        { fingerprint: tier, choice: 'custom', value: '9' },
      ],
    });

    const outcome = await api.proposals.merge('supply', '41');
    expect(outcome.status).toBe('conflicts');
    if (outcome.status !== 'conflicts') return;
    const refused = conflictOf(outcome.attempt, 'DV1');
    expect(refused.details['rejected']).toEqual(expect.stringContaining('Must be one of: 1, 2, 3'));
    expect(outcome.attempt.resolutions.some((r) => r.fingerprint === tier)).toBe(false);
    expect(outcome.attempt.resolutions).toHaveLength(5);

    await api.conflicts.resolve('supply', '41', {
      fingerprint: tier,
      choice: 'custom',
      value: '2',
    });
    expect((await api.proposals.merge('supply', '41')).status).toBe('merged');
  });

  it('commits a merge Revision with both parents and applies every Resolution', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    await api.conflicts.resolveMany('supply', '41', { resolutions: fullResolutions(attempt) });

    const outcome = await api.proposals.merge('supply', '41');
    expect(outcome.status).toBe('merged');
    if (outcome.status !== 'merged') return;
    const revision = await api.revisions.get('supply', outcome.revision);
    expect(revision.parents).toEqual([attempt.ours, attempt.theirs]);
    expect(revision.origin).toEqual({ type: 'merge', ref: '41' });
    expect(revision.stats.changes).toBeGreaterThan(0);
    expect(outcome.proposal).toMatchObject({ status: 'merged', merged_revision: revision.id });

    const main = { ...supply, branch: 'main' };
    const s1003 = await api.records.get(main, 'Supplier', 'S-1003');
    expect(s1003.record.attributes['tier']).toBe('1');
    const s1004 = await api.records.get(main, 'Supplier', 'S-1004');
    expect(s1004.record.attributes['risk_band']).toBe('medium');
    const monza = await api.records.get(main, 'Plant', 'MNZ');
    expect(monza.record.attributes['name']).toBe('Monza Foundry');
    const links = await api.records.links(main, 'Plant', 'MNZ', 'fed_by');
    expect(links.links.map((l) => l.ref).sort()).toEqual(['Supplier/S-1003', 'Supplier/S-1006']);

    const schema = await api.schema.get(main);
    const score = Object.values(
      schema.kinds.find((k) => k.api_name === 'Supplier')?.attributes ?? {},
    ).find((a) => a.api_name === 'risk_score');
    expect(score?.description).toBe('Risk score from the risk model, 0 to 60.');
    expect(score?.constraints.max).toBe(100);

    const kept = await api.conflicts.list('supply', '41');
    expect(kept.resolutions).toHaveLength(6);
    expect(kept.ours).toBe(attempt.ours);
  });

  it('drops the Links of a dangling Conflict when the answer is drop', async () => {
    const api = setup();
    const attempt = await api.conflicts.list('supply', '41');
    const resolutions = fullResolutions(attempt).map((r) =>
      r.fingerprint === conflictOf(attempt, 'XS9').fingerprint
        ? { ...r, choice: 'drop' as const }
        : r,
    );
    await api.conflicts.resolveMany('supply', '41', { resolutions });
    await api.proposals.merge('supply', '41');
    const links = await api.records.links({ ...supply, branch: 'main' }, 'Plant', 'MNZ', 'fed_by');
    expect(links.links).toEqual([]);
  });

  it('merges a Proposal without Conflicts straight away', async () => {
    const api = setup();
    const outcome = await api.proposals.merge('supply', '42');
    expect(outcome.status).toBe('merged');
  });
});
