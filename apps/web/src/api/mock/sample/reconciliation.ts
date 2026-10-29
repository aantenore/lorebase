import type {
  ClaimEvidence,
  ReconciliationCandidate,
  ReconciliationClaim,
} from '../../reconciliation-types';
import type { JsonValue } from '../../types';
import { DOC_REVIEW, fakeDigest, REVIEW_TEXT } from './documents';

/** Evidence for a quote of a document: the offsets are found in the text, so they cannot drift. */
function evidence(document: string, text: string, quote: string, chunk: number): ClaimEvidence {
  const start = text.indexOf(quote);
  if (start < 0) throw new Error(`The quote is not in ${document}: ${quote}`);
  return { document_id: document, chunk, start, end: start + quote.length, quote };
}

const reviewRow = (name: string) =>
  REVIEW_TEXT.split('\n').find((line) => line.includes(name)) ?? name;

const ERP = 'source:src_erp_vendors';

function claim(
  id: string,
  value: JsonValue,
  source: string,
  at: string,
  extra: Partial<ReconciliationClaim> = {},
): ReconciliationClaim {
  return {
    claim_id: id,
    value,
    value_hash: fakeDigest(JSON.stringify(value)).slice(0, 16),
    source,
    provenance_id: extra.proposal_item === undefined ? `prv_${id}` : null,
    proposal_item: null,
    evidence: null,
    valid_from: null,
    valid_to: null,
    at,
    ...extra,
  };
}

type Draft = Pick<ReconciliationCandidate, 'id' | 'type' | 'kind' | 'keys'> &
  Partial<ReconciliationCandidate>;

function candidate(draft: Draft): ReconciliationCandidate {
  const claims = draft.claims ?? [];
  return {
    branch: 'main',
    status: 'open',
    kind_id: `knd_${(draft.kind ?? '').toLowerCase()}`,
    attribute: null,
    attribute_id:
      draft.attribute === undefined || draft.attribute === null
        ? null
        : `att_${(draft.kind ?? '').toLowerCase()}_${draft.attribute}`,
    claims,
    claim_count: claims.length,
    signals: {},
    proposal_id: null,
    decision: null,
    decided_by: null,
    decided_at: null,
    created_at: '2026-10-20T08:05:00Z',
    updated_at: '2026-10-20T08:05:00Z',
    ...draft,
  };
}

/**
 * The reconciliation queue of the supply Domain: contradictions between the ERP and the
 * documents of Proposal 44, unit and negation differences between site surveys, superseded
 * payment terms with validity periods, stale values and probable duplicate Records.
 */
export function buildReconciliation(): Record<string, ReconciliationCandidate[]> {
  return {
    supply: [
      candidate({
        id: 'rcn_risk_s1025',
        type: 'contradiction',
        kind: 'Supplier',
        keys: ['S-1025'],
        attribute: 'risk_score',
        proposal_id: '44',
        created_at: '2026-10-20T08:01:30Z',
        claims: [
          claim('clm_s1025_erp', 30, ERP, '2026-10-12T02:04:00Z'),
          claim('clm_s1025_review', 74, `document:${DOC_REVIEW}`, '2026-10-20T08:01:12Z', {
            proposal_item: { proposal_id: '44', item_id: 'pit_11' },
            evidence: evidence(DOC_REVIEW, REVIEW_TEXT, reviewRow('S-1025'), 0),
          }),
        ],
      }),
      candidate({
        id: 'rcn_risk_s1013',
        type: 'contradiction',
        kind: 'Supplier',
        keys: ['S-1013'],
        attribute: 'risk_score',
        proposal_id: '44',
        created_at: '2026-10-20T08:01:29Z',
        claims: [
          claim('clm_s1013_erp', 12, ERP, '2026-10-12T02:04:00Z'),
          claim('clm_s1013_review', 39, `document:${DOC_REVIEW}`, '2026-10-20T08:01:12Z', {
            proposal_item: { proposal_id: '44', item_id: 'pit_12' },
            evidence: evidence(DOC_REVIEW, REVIEW_TEXT, reviewRow('S-1013'), 0),
          }),
        ],
      }),
      candidate({
        id: 'rcn_note_trn',
        type: 'contradiction',
        kind: 'Plant',
        keys: ['TRN'],
        attribute: 'logistics_note',
        signals: { unit: true },
        created_at: '2026-10-19T16:40:00Z',
        claims: [
          claim(
            'clm_trn_survey',
            'Rail terminal 5 km from the gate.',
            'import:survey_2026_09',
            '2026-09-30T10:00:00Z',
          ),
          claim(
            'clm_trn_logistics',
            'Rail terminal 7 km from the gate.',
            'principal:usr_sofia',
            '2026-10-19T16:39:00Z',
          ),
        ],
      }),
      candidate({
        id: 'rcn_note_glw',
        type: 'contradiction',
        kind: 'Plant',
        keys: ['GLW'],
        attribute: 'logistics_note',
        signals: { negation: true },
        created_at: '2026-10-19T16:41:00Z',
        claims: [
          claim(
            'clm_glw_survey',
            'Hazardous goods are stored on site.',
            'principal:usr_sofia',
            '2026-09-30T10:00:00Z',
          ),
          claim(
            'clm_glw_audit',
            'Hazardous goods are not stored on site.',
            'principal:usr_marco',
            '2026-10-18T09:12:00Z',
          ),
        ],
      }),
      candidate({
        id: 'rcn_note_mnz',
        type: 'contradiction',
        kind: 'Plant',
        keys: ['MNZ'],
        attribute: 'logistics_note',
        signals: {
          equivalent: true,
          similarity: 0.94,
          judge: { label: 'no contradiction', score: 0.12, model: 'qwen2.5:7b-instruct' },
        },
        created_at: '2026-10-19T16:42:00Z',
        claims: [
          claim(
            'clm_mnz_survey',
            'Night deliveries accepted after 22:00.',
            'import:survey_2026_09',
            '2026-09-30T10:00:00Z',
          ),
          claim(
            'clm_mnz_desk',
            'Deliveries at night are accepted after 22:00.',
            'principal:usr_sofia',
            '2026-10-17T14:05:00Z',
          ),
        ],
      }),
      candidate({
        id: 'rcn_terms_c2231',
        type: 'superseded',
        kind: 'Contract',
        keys: ['C-2231'],
        attribute: 'payment_terms_days',
        signals: { periods: true },
        created_at: '2026-10-18T07:30:00Z',
        claims: [
          claim('clm_c2231_import', 60, 'import:contracts_2022', '2022-04-01T08:00:00Z', {
            valid_from: '2022-04-01',
            valid_to: '2025-03-31',
          }),
          claim('clm_c2231_amendment', 45, 'principal:usr_marco', '2026-10-18T07:28:00Z', {
            valid_from: '2025-04-01',
            valid_to: '2027-03-31',
          }),
        ],
      }),
      candidate({
        id: 'rcn_terms_c2232',
        type: 'superseded',
        kind: 'Contract',
        keys: ['C-2232'],
        attribute: 'payment_terms_days',
        signals: { periods: true },
        created_at: '2026-10-18T07:31:00Z',
        claims: [
          claim('clm_c2232_import', 30, 'import:contracts_2022', '2022-01-10T08:00:00Z', {
            valid_from: '2022-01-01',
            valid_to: '2024-12-31',
          }),
          claim('clm_c2232_desk', 45, 'principal:usr_marco', '2026-10-18T07:29:00Z', {
            valid_from: '2025-01-01',
          }),
        ],
      }),
      candidate({
        id: 'rcn_stale_s1006',
        type: 'stale',
        kind: 'Supplier',
        keys: ['S-1006'],
        attribute: 'risk_score',
        signals: { stale: ['max_age'] },
        created_at: '2026-10-16T03:00:00Z',
        claims: [claim('clm_s1006_erp', 64, ERP, '2025-03-02T02:04:00Z')],
      }),
      candidate({
        id: 'rcn_stale_s1003',
        type: 'stale',
        kind: 'Supplier',
        keys: ['S-1003'],
        attribute: 'tier',
        signals: { stale: ['pending_claim'] },
        created_at: '2026-10-16T03:00:01Z',
        claims: [
          claim('clm_s1003_erp', '3', ERP, '2025-01-10T02:04:00Z'),
          claim('clm_s1003_run7', '2', ERP, '2026-10-15T02:04:00Z', {
            proposal_item: { proposal_id: '43', item_id: 'sync_s1003_tier' },
          }),
        ],
      }),
      candidate({
        id: 'rcn_dup_pl14_pl15',
        type: 'duplicate',
        kind: 'Plant',
        keys: ['PL-14', 'PL-15'],
        signals: { trigram: { attribute: 'att_plant_name', similarity: 0.82 } },
        created_at: '2026-10-16T03:00:02Z',
      }),
      candidate({
        id: 'rcn_dup_s1003_s1019',
        type: 'duplicate',
        kind: 'Supplier',
        keys: ['S-1003', 'S-1019'],
        signals: { ambiguous: true },
        proposal_id: '44',
        created_at: '2026-10-20T08:01:31Z',
      }),
      candidate({
        id: 'rcn_dup_s1013_s1025',
        type: 'duplicate',
        kind: 'Supplier',
        keys: ['S-1013', 'S-1025'],
        signals: { semantic: { similarity: 0.93 } },
        created_at: '2026-10-16T03:00:04Z',
      }),
      candidate({
        id: 'rcn_dup_c2233_c2234',
        type: 'duplicate',
        kind: 'Contract',
        keys: ['C-2233', 'C-2234'],
        status: 'dismissed',
        signals: { trigram: { attribute: 'att_contract_title', similarity: 0.81 } },
        decision: {
          action: 'dismiss',
          reason: 'Different Suppliers and terms; the titles only look alike.',
        },
        decided_by: 'usr_sofia',
        decided_at: '2026-10-17T11:00:00Z',
        created_at: '2026-10-16T03:00:03Z',
        updated_at: '2026-10-17T11:00:00Z',
      }),
      candidate({
        id: 'rcn_risk_s1019',
        type: 'contradiction',
        kind: 'Supplier',
        keys: ['S-1019'],
        attribute: 'risk_score',
        status: 'resolved',
        proposal_id: '44',
        claims: [
          claim('clm_s1019_erp', 18, ERP, '2026-10-12T02:04:00Z'),
          claim('clm_s1019_review', 24, `document:${DOC_REVIEW}`, '2026-10-20T08:01:12Z', {
            proposal_item: { proposal_id: '44', item_id: 'pit_13' },
            evidence: evidence(DOC_REVIEW, REVIEW_TEXT, reviewRow('S-1019'), 0),
          }),
        ],
        decision: {
          choice: 'keep_current',
          claim_id: 'clm_s1019_erp',
          reason: 'The ERP score already includes the quality notice.',
        },
        decided_by: 'usr_marco',
        decided_at: '2026-10-20T10:00:00Z',
        created_at: '2026-10-20T08:01:28Z',
        updated_at: '2026-10-20T10:00:00Z',
      }),
    ],
  };
}
