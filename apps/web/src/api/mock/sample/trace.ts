import type { TraceStep } from '../../types';

/** Agent sessions of the sample installation: the steps each one took, in order. */
export interface TraceSeed {
  /** Session id to its steps; the session list is derived from them. */
  sessions: Record<string, TraceStep[]>;
}

const AGENT = 'svc_herald';
const SESSION = 'sess_20261020_brevik';
const OTHER_SESSION = 'inferred-3f9a1c07d2b84e65';

const at = (time: string) => `2026-10-20T${time}Z`;

const rec = (kind: string, key: string) => ({ kind, key });
const link = (relation: string, from: [string, string], to: [string, string]) => ({
  relation,
  from: rec(...from),
  to: rec(...to),
});

const call = (
  step: number,
  time: string,
  fields: Partial<TraceStep> & Pick<TraceStep, 'tool' | 'method' | 'route'>,
): TraceStep => ({
  step,
  seq: 100 + step,
  type: 'call',
  at: at(time),
  actor: AGENT,
  principal_type: 'service',
  domain_id: 'supply',
  inferred: false,
  withheld: false,
  status: 200,
  branch: 'main',
  request_id: `req_${step.toString().padStart(4, '0')}`,
  trace_id: `4bf92f3577b34da6a3ce929d0e0e47${step.toString().padStart(2, '0')}`,
  ...fields,
});

const note = (step: number, time: string, text: string, agentStep?: number): TraceStep => ({
  step,
  seq: 100 + step,
  type: 'note',
  at: at(time),
  actor: AGENT,
  principal_type: 'service',
  domain_id: 'supply',
  inferred: false,
  withheld: false,
  note: text,
  ...(agentStep === undefined ? {} : { result: { agent_step: agentStep } }),
});

const redacted = { redacted: 'string' };

/**
 * A session over the sample `supply` Domain: a misspelled search, a note, a traversal, a context
 * pack within a budget, a Record explanation and a Proposal.
 */
function brevik(): TraceStep[] {
  return [
    call(1, '09:14:02', {
      tool: 'search',
      method: 'POST',
      route: '/api/v1/domains/{domain_id}/search',
      latency_ms: 21,
      cache: 'miss',
      query: { body: { q: 'brevik beerings', mode: 'lexical', limit: 5 } },
      result: {
        counts: { items: 3 },
        mode: 'lexical',
        corrections: [{ word: 'beerings', replacements: ['bearings'] }],
      },
      refs: {
        records: [rec('Supplier', 'S-1001'), rec('Part', 'P-77812'), rec('Part', 'P-77813')],
        links: [],
        truncated: false,
      },
      explain: {
        plans: [
          {
            label: 'search:lexical',
            node: 'Limit',
            rows: 3,
            cost: 14.2,
            node_types: ['Limit', 'Sort', 'Bitmap Heap Scan', 'Bitmap Index Scan'],
            indexes: ['mat_records_search_idx'],
            scans: [
              { node: 'Bitmap Heap Scan', relation: 'mat_records' },
              { node: 'Bitmap Index Scan', index: 'mat_records_search_idx' },
            ],
          },
        ],
      },
    }),
    note(
      2,
      '09:14:09',
      'Brevik Bearings S.p.A. is the Supplier meant. Check which Parts it is the only source of, and which Plants it feeds.',
      1,
    ),
    call(3, '09:14:12', {
      tool: 'traverse',
      method: 'POST',
      route: '/api/v1/domains/{domain_id}/traverse',
      latency_ms: 34,
      cache: 'bypass',
      query: {
        body: {
          start: { kind: 'Supplier', key: 'S-1001' },
          path: [{ relation: 'supplies' }, { relation: 'supplied_by' }],
        },
      },
      result: { counts: { items: 4, hops: 2 }, truncated: false },
      refs: {
        records: [
          rec('Supplier', 'S-1001'),
          rec('Part', 'P-77812'),
          rec('Part', 'P-77813'),
          rec('Supplier', 'S-1005'),
        ],
        links: [
          link('supplied_by', ['Part', 'P-77812'], ['Supplier', 'S-1001']),
          link('supplied_by', ['Part', 'P-77813'], ['Supplier', 'S-1001']),
          link('supplied_by', ['Part', 'P-77812'], ['Supplier', 'S-1005']),
        ],
        truncated: false,
      },
    }),
    call(4, '09:14:20', {
      tool: 'context',
      method: 'POST',
      route: '/api/v1/domains/{domain_id}/context',
      latency_ms: 87,
      cache: 'miss',
      query: { body: { search: 'brevik bearings', budget_tokens: 1200, depth: 2 } },
      result: {
        counts: { seeds: 1, neighbors: 5, links: 6 },
        budget: { limit_tokens: 1200, used_tokens: 1042, over_budget: false, capped: false },
        truncated: false,
      },
      refs: {
        records: [
          rec('Supplier', 'S-1001'),
          rec('Part', 'P-77812'),
          rec('Part', 'P-77813'),
          rec('Supplier', 'S-1005'),
          rec('Plant', 'TRN'),
          rec('Contract', 'C-2231'),
        ],
        links: [
          link('supplied_by', ['Part', 'P-77812'], ['Supplier', 'S-1001']),
          link('supplied_by', ['Part', 'P-77813'], ['Supplier', 'S-1001']),
          link('supplied_by', ['Part', 'P-77812'], ['Supplier', 'S-1005']),
          link('feeds', ['Supplier', 'S-1001'], ['Plant', 'TRN']),
          link('with_supplier', ['Contract', 'C-2231'], ['Supplier', 'S-1001']),
        ],
        truncated: false,
      },
    }),
    call(5, '09:14:31', {
      tool: 'explain',
      method: 'GET',
      route: '/api/v1/domains/{domain_id}/kinds/{kind}/records/{key}/explain',
      latency_ms: 12,
      cache: 'hit',
      query: { params: { branch: 'main' } },
      result: { counts: { links: 3, proposals: 0 } },
      refs: {
        records: [rec('Supplier', 'S-1001')],
        links: [],
        truncated: false,
      },
    }),
    note(
      6,
      '09:14:40',
      'P-77813 has a single source (S-1001). Open a Proposal that flags the Supplier so a person reviews it. <b>Not</b> merging anything myself.',
      3,
    ),
    call(7, '09:14:47', {
      tool: 'propose',
      method: 'POST',
      route: '/api/v1/domains/{domain_id}/proposals',
      status: 201,
      latency_ms: 58,
      cache: 'bypass',
      query: {
        body: {
          source_ref: 'model/risk',
          target_ref: 'main',
          title: redacted,
        },
      },
      result: { counts: { items: 1 } },
      proposal_id: '41',
      refs: { records: [rec('Supplier', 'S-1001')], links: [], truncated: false },
    }),
  ];
}

function hr(): TraceStep[] {
  const person = (step: number, time: string) =>
    call(step, time, {
      tool: 'search',
      method: 'POST',
      route: '/api/v1/domains/{domain_id}/search',
      domain_id: 'hr',
      actor: 'usr_marco',
      principal_type: 'user',
      inferred: true,
      latency_ms: 15 + step,
      cache: 'miss',
      query: { body: { q: redacted, limit: 10 } },
      result: { counts: { items: 4 }, mode: 'lexical' },
      refs: null,
    });
  return [person(1, '16:02:10'), person(2, '16:02:44')].map((s) => ({
    ...s,
    at: s.at.replace('2026-10-20', '2026-10-21'),
  }));
}

export function buildTraceSeed(): TraceSeed {
  return { sessions: { [SESSION]: brevik(), [OTHER_SESSION]: hr() } };
}
