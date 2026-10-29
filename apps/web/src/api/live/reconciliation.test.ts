import type { components } from '../generated/keeper';
import { bulkBody, candidateQuery, mapCandidate, mapDecision, resolveBody } from './reconciliation';

type S = components['schemas'];

const wire: S['ReconciliationCandidateOut'] = {
  id: 'rcn_1',
  branch: 'main',
  type: 'contradiction',
  status: 'open',
  kind: 'Supplier',
  kind_id: 'knd_1',
  keys: ['K-1'],
  attribute: 'score',
  attribute_id: 'att_1',
  claims: [
    {
      claim_id: 'c1',
      value: 5,
      source: 'source:src_1',
      provenance_id: 'prv_1',
      valid_from: '2025-01-01',
    },
    {
      claim_id: 'c2',
      value: '7 km',
      source: 'document:doc_1',
      proposal_item: { proposal_id: '9', item_id: 'it_1' },
      evidence: { document_id: 'doc_1', chunk: 0, start: 3, end: 7, quote: '7 km' },
    },
    { claim_id: 'c3', value: null, evidence: { document_id: 'doc_1', quote: 'no offsets' } },
  ],
  claim_count: 3,
  assignees_truncated: false,
  signals: { unit: true },
  proposal_id: null,
  decision: null,
  decided_by: null,
  decided_at: null,
  created_at: '2026-10-20T08:00:00Z',
  updated_at: '2026-10-20T08:00:00Z',
};

describe('reconciliation mapping', () => {
  it('makes the optional claim members explicit and keeps well-formed evidence only', () => {
    const [first, second, third] = mapCandidate(wire).claims;
    expect(first).toEqual({
      claim_id: 'c1',
      value: 5,
      value_hash: null,
      source: 'source:src_1',
      provenance_id: 'prv_1',
      proposal_item: null,
      evidence: null,
      valid_from: '2025-01-01',
      valid_to: null,
      at: null,
    });
    expect(second?.proposal_item).toEqual({ proposal_id: '9', item_id: 'it_1' });
    expect(second?.evidence).toEqual({
      document_id: 'doc_1',
      chunk: 0,
      start: 3,
      end: 7,
      quote: '7 km',
    });
    expect(third?.evidence).toBeNull();
  });

  it('maps a decision with its defaults', () => {
    const decision = mapDecision({ status: 'proposed', candidate: wire, proposal: '12' });
    expect(decision).toMatchObject({
      status: 'proposed',
      proposal: '12',
      revision: null,
      claim_id: null,
      edits: [],
      detail: null,
    });
  });

  it('sends only the given members and an explicit dry_run', () => {
    expect(resolveBody({ choice: 'claim', claim_id: 'c2', reason: 'r' })).toEqual({
      choice: 'claim',
      claim_id: 'c2',
      reason: 'r',
      dry_run: false,
    });
    expect(
      bulkBody({ filter: { type: 'stale' }, choice: 'dismiss', reason: 'r', dry_run: true }),
    ).toEqual({ filter: { type: 'stale' }, choice: 'dismiss', reason: 'r', dry_run: true });
    expect(candidateQuery({ kind: 'Supplier', status: 'open' })).toMatchObject({
      kind: 'Supplier',
      status: 'open',
      type: undefined,
    });
  });
});
