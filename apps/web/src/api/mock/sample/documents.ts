/**
 * The documents of the mock library: the four texts of `examples/supply/documents` (verbatim,
 * so item offsets stay valid against the real files) and a short PDF the older extraction
 * Proposal quotes. `text` is the parsed text the viewer shows and the item offsets index.
 */
import type { DocumentDetail } from '../../extraction-types';

export const AMENDMENT_TEXT = `# Amendment 2 to Framework Supply Agreement C-3013

Between Corvane Industries (buyer) and Wexrix Filtration Sp. z o.o., Wroclaw, Poland
(supplier, supplier code S-1013).

## Background

Agreement C-3013 started on 13 April 2023 and ends on 12 April 2026. Annual spend under the
agreement is about 976,000 PLN. Delivery is DAP, payment is due 30 days after invoice, and
the agreement does not renew automatically.

## Changes

1. The supplier adds part P-10033 (Needle roller bearing NK25) to the agreement. It is
   delivered to the Dresden Assembly plant (PL-14) and the Ostrava Assembly plant (PL-12).
2. The agreed unit price for P-10033 is 16.30 PLN for orders of 200 units or more, valid
   until the end date of the agreement.
3. The supplier keeps a safety stock of 1,000 units of P-10033 at its own warehouse and
   confirms stock levels every month.
4. A quality hold clause is added: the buyer may refuse a lot with more than 0.2 percent
   defective units and the supplier replaces it at its own cost within 15 working days.

## Unchanged

All other terms of C-3013 remain in force. In particular the buyer's right to terminate for
cause with 30 days' notice is not affected.

P-10033 has no second source. Procurement should open a sourcing review at least 120 days
before 12 April 2026.
`;

export const CONTRACT_TEXT = `# Framework Supply Agreement C-3025

**Buyer:** Corvane Industries, purchasing entity of the Distribution division.
**Supplier:** Kavvik Filtration A.S., Izmir, Turkey (supplier code S-1025).

## 1. Scope

The supplier delivers filter and bearing components to the buyer's plants on the basis of
purchase orders issued under this agreement. The parts covered at signature are P-10012
(Temperature probe PT100 150 mm), P-10018 (Air filter cartridge 10 micron) and P-10023
(Needle roller bearing NK25). The supplier is the only approved source for these parts.

## 2. Term

The agreement starts on 8 February 2024 and ends on 7 February 2026. It does not renew
automatically. Either party who wants to continue must send a written proposal at least
90 days before the end date.

## 3. Prices and payment

The expected yearly volume is 1,596,000.00 EUR. Prices are fixed in euro for the first
twelve months and then follow the steel index with a yearly cap of 4 percent. Invoices are
due 60 days after receipt of a correct invoice.

## 4. Delivery

Delivery terms are DDP (Incoterms 2020) to the plants named in each purchase order.
Lead times are between 43 and 82 days depending on the part.

## 5. Penalties

Late delivery of more than 5 working days is charged at 0.5 percent of the order value per
started week, capped at 8 percent of the order value. Penalties are deducted from the next
invoice.

## 6. Termination

Either party may terminate for cause with 30 days' written notice if a material breach is
not remedied. The buyer may terminate with immediate effect if the supplier becomes
insolvent.

Approved on 23 January 2024 by contract-manager-1.
`;

export const NOTICE_TEXT = `# Quality notice QN-2025-114

**Part:** P-10016, Servo motor 11 kW
**Supplier:** Ulmton Castings S.L., Zaragoza, Spain (S-1019)
**Received at:** Poznan Warehouse (PL-11)
**Purchase order:** PO-200046, 500 units, received 27 October 2025

## Finding

During incoming inspection 14 of 60 sampled motors showed a cracked terminal housing. The
defect rate of 23 percent is far above the 2 percent limit. The housings are cast by the
supplier. No safety incident has been reported.

## Containment

- The remaining stock of 2,600 units at PL-11 is blocked.
- The supplier was asked for a root cause analysis within 10 working days.
- Orders from other plants are served from stock at PL-11 only after a 100 percent visual
  check of the housing.

## Commercial position

Agreement C-3019 allows the buyer to reject non-conforming lots and to claim a replacement
within 15 working days or a credit note for the full lot value, 2,033.68 EUR per unit under
PO-200046. Delivery terms are EXW, so freight for returned lots is at the supplier's cost
only when the claim is accepted.

## Follow-up

The supplier was set to terminated in the ERP after this notice. P-10016 has no other
approved source; Procurement Risk to raise a critical flag on S-1019 and start the search
for an alternative.
`;

export const REVIEW_TEXT = `# Supplier review, fourth quarter 2025

Prepared by Procurement Risk for the quarterly sourcing meeting.

## Summary

Three suppliers need attention before the end of the first quarter of 2026. All three are
sole sources for at least one part and their framework agreements end soon.

| Supplier | Code | Risk score | Agreement | Ends | Sole-source parts |
|----------|------|-----------|-----------|------|-------------------|
| Kavvik Filtration A.S. | S-1025 | 74 | C-3025 | 7 Feb 2026 | P-10012, P-10018, P-10023 |
| Ulmton Castings S.L. | S-1019 | 24 | C-3019 | 9 Feb 2026 | P-10016 |
| Wexrix Filtration Sp. z o.o. | S-1013 | 39 | C-3013 | 12 Apr 2026 | P-10033 |

## Kavvik Filtration A.S.

Delivery performance fell from 94 percent on time in the second quarter to 81 percent in
the fourth. Purchase order PO-200031 for 1,000 units of P-10018 is still in transit and is
due on 1 February 2026, a week before the agreement ends. The supplier has asked for a
price increase of 6 percent, above the 4 percent cap in clause 3 of C-3025.

Recommendation: raise the risk score, open a Proposal to flag the supplier as high risk and
start qualification of a second source for P-10012 and P-10023.

## Ulmton Castings S.L.

The supplier status is terminated in the ERP but agreement C-3019 is still marked active.
Purchase order PO-200046 for 500 units of P-10016 was received in October 2025 and no new
orders are open.

Recommendation: close the agreement record and move P-10016 to a new supplier.

## Wexrix Filtration Sp. z o.o.

No open findings. Quality hold clause from Amendment 2 was not triggered in the quarter.
`;

export const OCTOBER_TEXT = `Contracts batch, October 2026

Contract C-2235. Orlova sealing framework agreement with Orlova Seals Sp. z o.o. (supplier S-1004).
This Agreement expires on 30 September 2027 unless renewed.

Contract C-2236. Valmora hydraulics supply terms with Valmora Hydraulics S.A. (supplier S-1006).
Term ends 31 January 2028.
`;

/** A document of the mock library: its metadata, jobs and the parsed text (null: none yet). */
export interface MockDocument {
  detail: DocumentDetail;
  text: string | null;
}

/** A stable 64-digit hex stand-in for a SHA-256 (the mock stores no bytes). */
export function fakeDigest(seed: string): string {
  let state = 2166136261;
  let out = '';
  for (let round = 0; out.length < 64; round += 1) {
    for (const char of `${round.toString()}:${seed}`) {
      state = Math.imul(state ^ char.charCodeAt(0), 16777619) >>> 0;
    }
    out += state.toString(16).padStart(8, '0');
  }
  return out.slice(0, 64);
}

const MARKDOWN = 'text/markdown';

function document(
  id: string,
  filename: string,
  mediaType: string,
  text: string | null,
  size: number,
  createdAt: string,
  jobs: DocumentDetail['extraction_jobs'],
  by = 'usr_sofia',
): MockDocument {
  return {
    text,
    detail: {
      id,
      filename,
      media_type: mediaType,
      sha256: fakeDigest(id),
      size,
      text_sha256: text === null ? null : fakeDigest(`text:${id}`),
      metadata: { source: 'upload' },
      created_at: createdAt,
      created_by: by,
      extraction_jobs: jobs,
    },
  };
}

export const DOC_CONTRACT = 'doc_contract_c3025';
export const DOC_AMENDMENT = 'doc_amendment_c3013';
export const DOC_NOTICE = 'doc_quality_notice_114';
export const DOC_REVIEW = 'doc_supplier_review_q4';
export const DOC_OCTOBER = 'doc_contracts_oct';
export const DOC_SCAN = 'doc_scan_1999';

const job44 = (finished: string) => ({
  id: 'job_44',
  status: 'succeeded',
  model: 'gliner-multi-v2.1 + qwen2.5:7b-instruct',
  prompt_version: 'extract-v1',
  branch: 'extract/job-44',
  proposal_id: '44',
  created_at: '2026-10-20T08:00:00Z',
  finished_at: finished,
  usage: { input_tokens: 5120, output_tokens: 412, cost: 0 },
  stats: { chunks: 9, candidates: 16, items: 13, dropped_unchanged: 3, dropped_unverified: 1 },
  error: null,
});

/** The library of the `supply` sample Domain, newest first. */
export function supplyDocuments(): MockDocument[] {
  return [
    document(
      DOC_CONTRACT,
      'contract-c-3025-kavvik-filtration.md',
      MARKDOWN,
      CONTRACT_TEXT,
      CONTRACT_TEXT.length,
      '2026-10-20T07:58:00Z',
      [job44('2026-10-20T08:01:12Z')],
    ),
    document(
      DOC_AMENDMENT,
      'amendment-c-3013-wexrix-filtration.md',
      MARKDOWN,
      AMENDMENT_TEXT,
      AMENDMENT_TEXT.length,
      '2026-10-20T07:58:10Z',
      [job44('2026-10-20T08:01:12Z')],
    ),
    document(
      DOC_NOTICE,
      'quality-notice-p-10016.md',
      MARKDOWN,
      NOTICE_TEXT,
      NOTICE_TEXT.length,
      '2026-10-20T07:58:20Z',
      [job44('2026-10-20T08:01:12Z')],
    ),
    document(
      DOC_REVIEW,
      'supplier-review-2025-q4.md',
      MARKDOWN,
      REVIEW_TEXT,
      REVIEW_TEXT.length,
      '2026-10-20T07:58:30Z',
      [job44('2026-10-20T08:01:12Z')],
    ),
    document(
      DOC_OCTOBER,
      'contracts-october.pdf',
      'application/pdf',
      OCTOBER_TEXT,
      48_213,
      '2026-10-19T11:10:00Z',
      [
        {
          id: 'job_42',
          status: 'succeeded',
          model: 'qwen2.5:7b-instruct',
          prompt_version: 'extract-v1',
          branch: 'extract/job-42',
          proposal_id: '42',
          created_at: '2026-10-19T11:12:00Z',
          finished_at: '2026-10-19T11:25:00Z',
          usage: { input_tokens: 2210, output_tokens: 190, cost: 0 },
          stats: { chunks: 2, candidates: 4, items: 4 },
          error: null,
        },
      ],
    ),
    document(
      DOC_SCAN,
      'scan-contract-1999.pdf',
      'application/pdf',
      null,
      311_902,
      '2026-10-18T15:02:00Z',
      [
        {
          id: 'job_41',
          status: 'failed',
          model: 'rules',
          prompt_version: 'extract-v1',
          branch: null,
          proposal_id: null,
          created_at: '2026-10-18T15:02:30Z',
          finished_at: '2026-10-18T15:02:41Z',
          usage: {},
          stats: {},
          error: {
            message: 'The PDF has no text layer; scanned documents are not supported (no OCR).',
          },
        },
      ],
      'usr_marco',
    ),
  ];
}
