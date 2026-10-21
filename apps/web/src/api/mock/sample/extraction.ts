import type { ExtractionItem, ItemEvidence, ItemMatch } from '../../extraction-types';
import {
  AMENDMENT_TEXT,
  CONTRACT_TEXT,
  DOC_AMENDMENT,
  DOC_CONTRACT,
  DOC_NOTICE,
  DOC_OCTOBER,
  DOC_REVIEW,
  NOTICE_TEXT,
  OCTOBER_TEXT,
  REVIEW_TEXT,
} from './documents';

/** Evidence for a quote of a document: the offsets are found in the text, so they cannot drift. */
function evidence(document: string, text: string, quote: string, chunk: number): ItemEvidence {
  const start = text.indexOf(quote);
  if (start < 0) throw new Error(`The quote is not in ${document}: ${quote}`);
  return { document_id: document, chunk, start, end: start + quote.length, quote };
}

const exact = (): ItemMatch => ({ status: 'matched', method: 'key', score: 1 });

type Draft = Partial<ExtractionItem> & Pick<ExtractionItem, 'type' | 'evidence'>;

let seq = 0;
function item(id: string, draft: Draft): ExtractionItem {
  seq += 1;
  return {
    id,
    seq,
    decision: 'pending',
    confidence: null,
    tier: 0,
    model: null,
    kind: null,
    relation: null,
    key: null,
    attribute: null,
    value: null,
    current: null,
    from: null,
    to: null,
    values: {},
    match: null,
    edits: null,
    decided_by: null,
    decided_at: null,
    not_applied: null,
    ...draft,
  };
}

/**
 * The items of Proposal 44, extracted from the four documents of `examples/supply/documents`
 * with rules and the local models, and one value by the chat model that was accepted at once.
 * The first two items are the output shown in docs/extraction.md.
 */
export function supplyItems(): ExtractionItem[] {
  seq = 0;
  const supplierLine =
    '**Supplier:** Kavvik Filtration A.S., Izmir, Turkey (supplier code S-1025).';
  const plants =
    'It is\n   delivered to the Dresden Assembly plant (PL-14) and the Ostrava Assembly plant (PL-12).';
  const row = (name: string) => REVIEW_TEXT.split('\n').find((line) => line.includes(name)) ?? name;
  return [
    item('pit_01', {
      type: 'record',
      confidence: 0.425,
      kind: 'Contract',
      key: 'C-3025',
      values: {
        contract_id: 'C-3025',
        title: 'Framework Supply Agreement C-3025',
        start_date: '2024-02-08',
        expiry_date: '2026-02-07',
        annual_value: '1596000.00',
        currency: 'EUR',
        incoterm: 'DDP',
        payment_terms_days: '60',
      },
      match: {
        status: 'new',
        method: 'key_pattern',
        score: 1,
        complete: true,
        missing: [],
        missing_links: ['with_supplier'],
      },
      evidence: evidence(DOC_CONTRACT, CONTRACT_TEXT, 'Framework Supply Agreement C-3025', 0),
    }),
    item('pit_02', {
      type: 'link',
      confidence: 0.6,
      relation: 'with_supplier',
      from: 'C-3025',
      to: 'S-1025',
      match: exact(),
      evidence: evidence(DOC_CONTRACT, CONTRACT_TEXT, supplierLine, 0),
    }),
    item('pit_03', {
      type: 'value',
      confidence: 0.55,
      kind: 'Contract',
      key: 'C-3013',
      attribute: 'incoterm',
      value: 'DAP',
      current: null,
      match: exact(),
      evidence: evidence(
        DOC_AMENDMENT,
        AMENDMENT_TEXT,
        'Delivery is DAP, payment is due 30 days after invoice',
        1,
      ),
    }),
    item('pit_04', {
      type: 'value',
      confidence: 0.55,
      kind: 'Contract',
      key: 'C-3013',
      attribute: 'payment_terms_days',
      value: '30',
      current: null,
      match: exact(),
      evidence: evidence(DOC_AMENDMENT, AMENDMENT_TEXT, 'payment is due 30 days after invoice', 1),
    }),
    item('pit_05', {
      type: 'link',
      confidence: 0.5,
      relation: 'supplied_by',
      from: 'P-10033',
      to: 'S-1013',
      match: exact(),
      evidence: evidence(
        DOC_AMENDMENT,
        AMENDMENT_TEXT,
        'The supplier adds part P-10033 (Needle roller bearing NK25) to the agreement.',
        2,
      ),
    }),
    item('pit_06', {
      type: 'link',
      confidence: 0.42,
      tier: 1,
      model: 'glirel-large-v0',
      relation: 'feeds',
      from: 'S-1013',
      to: 'PL-14',
      match: {
        status: 'ambiguous',
        method: 'trigram',
        score: 0.91,
        candidates: [
          { key: 'PL-14', title: 'Dresden Assembly', score: 0.91, method: 'title' },
          { key: 'PL-15', title: 'Dresden Warehouse', score: 0.88, method: 'trigram' },
        ],
      },
      evidence: evidence(DOC_AMENDMENT, AMENDMENT_TEXT, plants, 2),
    }),
    item('pit_07', {
      type: 'link',
      confidence: 0.5,
      relation: 'feeds',
      from: 'S-1013',
      to: 'PL-12',
      match: exact(),
      evidence: evidence(DOC_AMENDMENT, AMENDMENT_TEXT, plants, 2),
    }),
    item('pit_08', {
      type: 'record',
      confidence: 0.62,
      tier: 1,
      model: 'gliner-multi-v2.1',
      kind: 'Part',
      key: 'P-10016',
      values: { part_number: 'P-10016', name: 'Servo motor 11 kW' },
      match: {
        status: 'new',
        method: 'key_pattern',
        score: 1,
        complete: true,
        missing: [],
        missing_links: [],
      },
      evidence: evidence(DOC_NOTICE, NOTICE_TEXT, '**Part:** P-10016, Servo motor 11 kW', 0),
    }),
    item('pit_09', {
      type: 'link',
      confidence: 0.58,
      tier: 1,
      model: 'glirel-large-v0',
      relation: 'supplied_by',
      from: 'P-10016',
      to: 'S-1019',
      match: exact(),
      evidence: evidence(
        DOC_NOTICE,
        NOTICE_TEXT,
        '**Supplier:** Ulmton Castings S.L., Zaragoza, Spain (S-1019)',
        0,
      ),
    }),
    item('pit_10', {
      type: 'value',
      confidence: 0.31,
      kind: 'Supplier',
      key: 'S-1019',
      attribute: 'risk_score',
      value: '2',
      current: '18',
      decision: 'rejected',
      decided_by: 'usr_sofia',
      decided_at: '2026-10-20T09:12:00Z',
      match: exact(),
      evidence: evidence(DOC_NOTICE, NOTICE_TEXT, 'far above the 2 percent limit', 1),
    }),
    item('pit_11', {
      type: 'value',
      confidence: 0.78,
      kind: 'Supplier',
      key: 'S-1025',
      attribute: 'risk_score',
      value: '74',
      current: '30',
      match: exact(),
      evidence: evidence(DOC_REVIEW, REVIEW_TEXT, row('Kavvik Filtration A.S. | S-1025'), 0),
    }),
    item('pit_12', {
      type: 'value',
      confidence: 0.78,
      kind: 'Supplier',
      key: 'S-1013',
      attribute: 'risk_score',
      value: '39',
      current: '12',
      match: exact(),
      evidence: evidence(DOC_REVIEW, REVIEW_TEXT, row('Wexrix Filtration Sp. z o.o. | S-1013'), 0),
    }),
    item('pit_13', {
      type: 'value',
      confidence: 0.93,
      tier: 2,
      model: 'qwen2.5:7b-instruct',
      kind: 'Supplier',
      key: 'S-1019',
      attribute: 'risk_score',
      value: '24',
      current: '18',
      decision: 'accepted',
      decided_by: 'svc_scribe',
      decided_at: '2026-10-20T08:01:12Z',
      match: exact(),
      evidence: evidence(DOC_REVIEW, REVIEW_TEXT, row('Ulmton Castings S.L. | S-1019'), 0),
    }),
  ];
}

/** The items of Proposal 42, all accepted: two Contracts and their Links from a PDF. */
export function octoberItems(): ExtractionItem[] {
  seq = 0;
  const accepted = {
    decision: 'accepted' as const,
    decided_by: 'svc_scribe',
    decided_at: '2026-10-19T11:25:00Z',
    tier: 2,
    model: 'qwen2.5:7b-instruct',
  };
  const newRecord: ItemMatch = {
    status: 'new',
    method: 'key_pattern',
    score: 1,
    complete: true,
    missing: [],
    missing_links: [],
  };
  const quote = (text: string) => evidence(DOC_OCTOBER, OCTOBER_TEXT, text, 0);
  return [
    item('pit_c1', {
      ...accepted,
      type: 'record',
      confidence: 0.94,
      kind: 'Contract',
      key: 'C-2235',
      values: {
        contract_id: 'C-2235',
        title: 'Orlova sealing framework',
        expiry_date: '2027-09-30',
      },
      match: newRecord,
      evidence: quote('Contract C-2235. Orlova sealing framework agreement'),
    }),
    item('pit_c2', {
      ...accepted,
      type: 'record',
      confidence: 0.92,
      kind: 'Contract',
      key: 'C-2236',
      values: {
        contract_id: 'C-2236',
        title: 'Valmora hydraulics supply',
        expiry_date: '2028-01-31',
      },
      match: newRecord,
      evidence: quote('Contract C-2236. Valmora hydraulics supply terms'),
    }),
    item('pit_c3', {
      ...accepted,
      type: 'link',
      confidence: 0.93,
      relation: 'with_supplier',
      from: 'C-2235',
      to: 'S-1004',
      match: exact(),
      evidence: quote('(supplier S-1004)'),
    }),
    item('pit_c4', {
      ...accepted,
      type: 'link',
      confidence: 0.92,
      relation: 'with_supplier',
      from: 'C-2236',
      to: 'S-1006',
      match: exact(),
      evidence: quote('(supplier S-1006)'),
    }),
  ];
}
