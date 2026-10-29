import type { ReconciliationCandidate } from '@/api/reconciliation-types';
import { buildReconciliation } from '@/api/mock/sample/reconciliation';
import {
  candidateTitle,
  countLabel,
  currentClaim,
  differingAttributes,
  periodOf,
  rankCandidates,
  sameValue,
  signalBadges,
  sourceLabel,
} from './reconcile-state';

const sample = buildReconciliation()['supply'] ?? [];
const byId = (id: string): ReconciliationCandidate => {
  const found = sample.find((c) => c.id === id);
  if (found === undefined) throw new Error(id);
  return found;
};

describe('reconcile state', () => {
  it('titles value and duplicate candidates', () => {
    expect(candidateTitle(byId('rcn_note_trn'))).toBe('Plant TRN · logistics_note');
    expect(candidateTitle(byId('rcn_dup_pl14_pl15'))).toBe('Plant PL-14 / PL-15');
  });

  it('turns every signal into a badge', () => {
    const labels = (signals: ReconciliationCandidate['signals']) =>
      signalBadges(signals).map((b) => b.label);
    expect(labels({ unit: true })).toEqual(['unit']);
    expect(labels({ negation: true, periods: true })).toEqual(['negation', 'periods']);
    expect(
      labels({
        equivalent: true,
        similarity: 0.9412,
        judge: { label: 'no contradiction', score: 0.12, model: 'm' },
      }),
    ).toEqual(['equivalent', 'judge: no contradiction 0.12', 'similarity 0.94']);
    expect(labels({ stale: ['max_age', 'period_ended'] })).toEqual([
      'stale: max_age, period_ended',
    ]);
    expect(labels({ trigram: { attribute: 'att_x', similarity: 0.82 } })).toEqual([
      'similar titles 0.82',
    ]);
    expect(labels({ semantic: { similarity: 0.93 }, ambiguous: true })).toEqual([
      'ambiguous mention',
      'similar meaning 0.93',
    ]);
    expect(labels({ newer: 1 })).toEqual(['newer']);
    expect(signalBadges({ judge: { label: 'x', model: 'qwen' } })[0]?.hint).toBe('qwen');
  });

  it('ranks negations first and equivalents last, keeping the order otherwise', () => {
    const ids = rankCandidates([
      byId('rcn_note_mnz'),
      byId('rcn_risk_s1025'),
      byId('rcn_note_glw'),
      byId('rcn_note_trn'),
    ]).map((c) => c.id);
    expect(ids).toEqual(['rcn_note_glw', 'rcn_risk_s1025', 'rcn_note_trn', 'rcn_note_mnz']);
  });

  it('names source identities', () => {
    const names = new Map([['src_erp', 'erp.vendors']]);
    expect(sourceLabel('source:src_erp', names)).toBe('Source erp.vendors');
    expect(sourceLabel('source:src_other', names)).toBe('Source src_other');
    expect(sourceLabel('document:doc_1')).toBe('Document doc_1');
    expect(sourceLabel('import:batch_7')).toBe('Import batch_7');
    expect(sourceLabel('merge_resolution')).toBe('merge_resolution');
    expect(sourceLabel(null)).toBe('unknown source');
  });

  it('compares decimal strings with numbers and finds the current claim', () => {
    expect(sameValue('30', 30)).toBe(true);
    expect(sameValue([1], [1])).toBe(true);
    expect(sameValue('a', 'b')).toBe(false);
    expect(currentClaim(byId('rcn_risk_s1025'), '30')?.claim_id).toBe('clm_s1025_erp');
    expect(currentClaim(byId('rcn_risk_s1025'), undefined)).toBeUndefined();
  });

  it('shows periods with open ends', () => {
    const [older, newer] = byId('rcn_terms_c2232').claims;
    expect(older && periodOf(older)).toBe('2022-01-01 to 2024-12-31');
    expect(newer && periodOf(newer)).toBe('2025-01-01 to open');
    expect(periodOf(byId('rcn_risk_s1025').claims[0]!)).toBeNull();
  });

  it('lists differing Attributes and counts past the limit', () => {
    expect(
      differingAttributes(['a', 'b', 'c'], { a: 1, b: 'x' }, { a: '1', b: 'y', c: 2 }),
    ).toEqual(['b', 'c']);
    expect(countLabel(3, false)).toBe('3');
    expect(countLabel(500, true)).toBe('500+');
  });
});
