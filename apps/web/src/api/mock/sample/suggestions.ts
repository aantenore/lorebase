import type { DocumentExample, SchemaSuggestion } from '../../suggestion-types';
import {
  AMENDMENT_TEXT,
  CONTRACT_TEXT,
  DOC_AMENDMENT,
  DOC_CONTRACT,
  DOC_NOTICE,
  NOTICE_TEXT,
} from './documents';

/** Evidence for a quote of a document: the offsets are found in the text, so they cannot drift. */
function example(document: string, text: string, quote: string, chunk: number): DocumentExample {
  const start = text.indexOf(quote);
  if (start < 0) throw new Error(`The quote is not in ${document}: ${quote}`);
  return {
    document_id: document,
    chunk,
    start,
    end: start + quote.length,
    quote,
  };
}

/** A pending vocabulary suggestion of the Supplier Kind, proposed from missed phrases: it comes
 * from no document. */
function vocabulary(
  id: string,
  type: SchemaSuggestion['type'],
  name: string,
  patch: Partial<SchemaSuggestion>,
): SchemaSuggestion {
  return {
    ...base,
    id,
    type,
    name,
    kind: 'Supplier',
    labels: [name],
    documents: 0,
    confidence: 0.6,
    status: 'pending',
    sources: ['resolve_misses'],
    support: 3,
    ...patch,
  };
}

const base = {
  kind: null,
  target: null,
  type_guess: null,
  duplicate_of: null,
  withheld: false,
  samples: [],
  examples: [],
  sources: ['rules'],
  proposal_id: null,
  branch: null,
  decided_by: null,
  decided_at: null,
  created_at: '2026-10-20T09:00:00Z',
  updated_at: '2026-10-20T09:00:00Z',
} satisfies Partial<SchemaSuggestion>;

/** Pending suggestions below this support are listed only with `all`. */
export const MOCK_MIN_SUPPORT = 2;

/** The suggestions of the supply Domain: a Kind, an Attribute and a Relation to the new Kind. */
export function buildSuggestions(): Record<string, SchemaSuggestion[]> {
  return {
    supply: [
      {
        ...base,
        id: 'sug_kind_quality_notice',
        type: 'kind',
        name: 'QualityNotice',
        labels: ['Quality notice', 'QN'],
        support: 5,
        documents: 2,
        confidence: 0.82,
        status: 'pending',
        samples: ['QN-2025-114', 'QN-2025-120'],
        examples: [
          example(DOC_NOTICE, NOTICE_TEXT, 'Quality notice QN-2025-114', 0),
          example(DOC_NOTICE, NOTICE_TEXT, 'During incoming inspection', 1),
        ],
        sources: ['rules', 'model'],
      },
      {
        ...base,
        id: 'sug_attr_supplier_ends',
        type: 'attribute',
        name: 'ends',
        kind: 'Supplier',
        labels: ['ends on', 'end date'],
        support: 4,
        documents: 2,
        confidence: 0.74,
        status: 'pending',
        type_guess: {
          type: 'date',
          constraints: {},
          confidence: 0.91,
          reasons: ['4 of 4 samples parse as dates', 'labels mention an end date'],
        },
        samples: ['7 February 2026', '31 March 2026'],
        examples: [
          example(DOC_CONTRACT, CONTRACT_TEXT, 'ends on 7 February 2026', 2),
          example(DOC_AMENDMENT, AMENDMENT_TEXT, 'Amendment 2', 0),
        ],
      },
      {
        ...base,
        id: 'sug_rel_notice_part',
        type: 'relation',
        name: 'part',
        kind: '+QualityNotice',
        target: 'Part',
        labels: ['concerns part'],
        support: 3,
        documents: 1,
        confidence: 0.66,
        status: 'pending',
        examples: [example(DOC_NOTICE, NOTICE_TEXT, '**Part:** P-10016', 0)],
      },
      {
        ...base,
        id: 'sug_attr_supplier_vat',
        type: 'attribute',
        name: 'vat_number',
        kind: 'Supplier',
        labels: ['VAT number'],
        support: 3,
        documents: 1,
        confidence: 0.58,
        status: 'pending',
        withheld: true,
        duplicate_of: { section: 'attributes', api_name: 'tax_id' },
      },
      {
        ...base,
        id: 'sug_kind_memo',
        type: 'kind',
        name: 'Memo',
        labels: ['memo'],
        support: 1,
        documents: 1,
        confidence: 0.31,
        status: 'pending',
      },
      vocabulary('sug_valias_paused', 'value_alias', 'paused', {
        attribute: 'status',
        value: 'on_hold',
        support: 4,
        examples: [{ phrase: 'paused', misses: 4, callers: 2, similarity: 0.61 }],
      }),
      vocabulary('sug_kalias_vendor', 'kind_alias', 'vendor', {
        support: 6,
        examples: [{ phrase: 'vendor', misses: 4, callers: 3, similarity: 0.72 }],
      }),
      vocabulary('sug_adesc_tier', 'attribute_description', 'tier', {
        attribute: 'tier',
        description: 'Importance: tier 1 is critical.',
        support: 3,
      }),
    ],
  };
}
