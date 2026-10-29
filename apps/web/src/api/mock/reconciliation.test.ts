import { ConflictError, ValidationError } from '../errors';
import { asRemote, createMockApi } from './adapter';

// Wrapped as the app wraps it, so a refusal is a rejected promise as over the wire.
const api = () => asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);

describe('mock reconciliation queue', () => {
  it('lists newest first with filters and cursor paging', async () => {
    const mock = api();
    const open = await mock.reconciliation.list('supply', {
      type: 'contradiction',
      status: 'open',
    });
    expect(open.items.map((c) => c.id)).toEqual([
      'rcn_risk_s1025',
      'rcn_risk_s1013',
      'rcn_note_mnz',
      'rcn_note_glw',
      'rcn_note_trn',
    ]);
    const bySource = await mock.reconciliation.list('supply', { source: 'principal:usr_marco' });
    expect(bySource.items.map((c) => c.id)).toEqual([
      'rcn_note_glw',
      'rcn_terms_c2232',
      'rcn_terms_c2231',
    ]);
    const first = await mock.reconciliation.list('supply', { type: 'duplicate', limit: 2 });
    expect(first.items).toHaveLength(2);
    expect(first.next_cursor).not.toBeNull();
    const rest = await mock.reconciliation.list('supply', {
      type: 'duplicate',
      limit: 2,
      cursor: first.next_cursor,
    });
    expect(rest.items.length).toBeGreaterThan(0);
    await expect(
      mock.reconciliation.list('supply', { attribute: 'risk_score' }),
    ).rejects.toBeInstanceOf(ValidationError);
  });

  it('turns a claim on the protected default Branch into a Proposal and keeps the candidate open', async () => {
    const mock = api();
    const decided = await mock.reconciliation.resolve('supply', 'rcn_risk_s1025', {
      choice: 'claim',
      claim_id: 'clm_s1025_review',
      reason: 'The quarterly review is newer.',
    });
    expect(decided.status).toBe('proposed');
    expect(decided.candidate.status).toBe('open');
    const proposal = await mock.proposals.get('supply', decided.proposal ?? '');
    expect(proposal.source_ref).toBe('reconcile/rcn_risk_s1025');
    const changed = await mock.records.get(
      { domain: 'supply', branch: proposal.source_ref },
      'Supplier',
      'S-1025',
    );
    expect(changed.record.attributes['risk_score']).toBe(74);
    const main = await mock.records.get({ domain: 'supply' }, 'Supplier', 'S-1025');
    expect(main.record.attributes['risk_score']).toBe(30);
  });

  it('previews without writing and keeps the current value without a Revision', async () => {
    const mock = api();
    const preview = await mock.reconciliation.resolve('supply', 'rcn_note_trn', {
      choice: 'policy',
      policy: 'newest_wins',
      reason: 'check',
      dry_run: true,
    });
    expect(preview.status).toBe('dry_run');
    expect(preview.claim_id).toBe('clm_trn_logistics');
    expect(preview.edits).toHaveLength(1);

    const kept = await mock.reconciliation.resolve('supply', 'rcn_note_trn', {
      choice: 'keep_current',
      reason: 'The survey is right.',
    });
    expect(kept.status).toBe('resolved');
    expect(kept.revision).toBeNull();
    expect(kept.candidate.decision?.['reason']).toBe('The survey is right.');
    await expect(
      mock.reconciliation.dismiss('supply', 'rcn_note_trn', 'late'),
    ).rejects.toBeInstanceOf(ConflictError);
  });

  it('needs a reason, reports an undecided policy, and reopens a dismissal', async () => {
    const mock = api();
    await expect(
      mock.reconciliation.dismiss('supply', 'rcn_note_glw', '  '),
    ).rejects.toBeInstanceOf(ValidationError);
    const undecided = await mock.reconciliation.resolve('supply', 'rcn_note_glw', {
      choice: 'policy',
      policy: 'authority',
      reason: 'rank',
    });
    expect(undecided.status).toBe('undecided');
    expect(undecided.detail).toMatch(/equally/);

    const dismissed = await mock.reconciliation.dismiss('supply', 'rcn_note_glw', 'Same site.');
    expect(dismissed.candidate.status).toBe('dismissed');
    const reopened = await mock.reconciliation.reopen('supply', 'rcn_note_glw', 'New audit.');
    expect(reopened.status).toBe('reopened');
    expect(reopened.candidate.status).toBe('open');
  });

  it('merges duplicates through a Proposal: survivor values, Links moved, absorbed deleted', async () => {
    const mock = api();
    const decided = await mock.reconciliation.resolve('supply', 'rcn_dup_pl14_pl15', {
      choice: 'merge',
      survivor: 'PL-14',
      values: { name: 'Dresden Assembly and Warehouse' },
      reason: 'One site.',
    });
    expect(decided.status).toBe('proposed');
    expect(decided.candidate.proposal_id).toBe(decided.proposal);
    const scope = { domain: 'supply', branch: decided.branch ?? '' };
    const survivor = await mock.records.get(scope, 'Plant', 'PL-14');
    expect(survivor.record.attributes['name']).toBe('Dresden Assembly and Warehouse');
    await expect(mock.records.get(scope, 'Plant', 'PL-15')).rejects.toThrow();
    await expect(
      mock.reconciliation.resolve('supply', 'rcn_dup_s1003_s1019', {
        choice: 'claim',
        reason: 'x',
      }),
    ).rejects.toBeInstanceOf(ValidationError);
  });

  it('decides in bulk and lists the open candidates of a Proposal in its check', async () => {
    const mock = api();
    const before = await mock.proposals.get('supply', '44');
    const check = before.checks.find((c) => c.name === 'reconciliation');
    expect(check?.candidates).toEqual(['rcn_dup_s1003_s1019', 'rcn_risk_s1025', 'rcn_risk_s1013']);

    const preview = await mock.reconciliation.resolveMany('supply', {
      filter: { type: 'contradiction', kind: 'Supplier' },
      choice: 'dismiss',
      reason: 'bulk',
      dry_run: true,
    });
    expect(preview.counts).toEqual({ dry_run: 2 });
    const done = await mock.reconciliation.resolveMany('supply', {
      ids: ['rcn_risk_s1025', 'rcn_risk_s1013'],
      choice: 'dismiss',
      reason: 'Reviewed with Procurement.',
    });
    expect(done.counts).toEqual({ dismissed: 2 });
    const after = await mock.proposals.get('supply', '44');
    expect(after.checks.find((c) => c.name === 'reconciliation')?.candidates).toEqual([
      'rcn_dup_s1003_s1019',
    ]);
    expect((await mock.reconciliation.sweep('supply')).branch).toBe('main');
  });
});
