import type { ContextPack, SearchResult } from '@/api/types';
import { readEmbedAnswer, parseVector } from '@/api/guide-client';
import { involvedRefs, labHighlight } from '@/features/canvas/lab-graph';
import { budgetReport, explainPack } from './pack-explain';
import { defaultParams, toRequest } from './params';
import { compareResults, describeRow } from './result-diff';

const hit = (ref: string): SearchResult => ({
  ref,
  kind: ref.split('/')[0] ?? '',
  title: ref,
  snippet: '',
  score: 1,
});

describe('compareResults', () => {
  const a = ['Part/1', 'Part/2', 'Part/3'].map(hit);
  const b = ['Part/2', 'Part/1', 'Part/4'].map(hit);

  it('counts the overlap and the rank changes', () => {
    const result = compareResults(a, b);
    expect(result).toMatchObject({ shared: 2, onlyA: 1, onlyB: 1, reordered: 2 });
    expect(result.rows.get('Part/1')).toEqual({ ref: 'Part/1', rankA: 1, rankB: 2, moved: -1 });
    expect(result.rows.get('Part/3')).toMatchObject({ rankA: 3, rankB: null, moved: null });
  });

  it('describes each Record from the side of the list it is in', () => {
    const { rows } = compareResults(a, b);
    const one = rows.get('Part/1');
    const two = rows.get('Part/2');
    if (one === undefined || two === undefined) throw new Error('missing rows');
    expect(describeRow(one, 'A')).toBe('ranks 1 higher than in B');
    expect(describeRow(one, 'B')).toBe('ranks 1 lower than in A');
    expect(describeRow(two, 'B')).toBe('ranks 1 higher than in A');
    expect(describeRow(rows.get('Part/3') as never, 'A')).toBe('only in A');
    expect(describeRow(rows.get('Part/4') as never, 'B')).toBe('only in B');
  });

  it('reports identical lists as unchanged', () => {
    const same = compareResults(a, a);
    expect(same).toMatchObject({ shared: 3, onlyA: 0, onlyB: 0, reordered: 0 });
    expect(describeRow(same.rows.get('Part/1') as never, 'A')).toBe('same rank in B');
  });
});

const pack: ContextPack = {
  domain: 'supply',
  branch: 'main',
  revision: 'r1',
  etag: 'W/"r1"',
  seeds: [{ ref: 'Supplier/S-1', title: 'Brevik', values: {} }],
  neighbors: [
    { ref: 'Part/P-1', title: 'Bearing', depth: 1, values: { name: 'Bearing' } },
    { ref: 'Plant/TRN', title: 'Torino', depth: 1, values: null },
    { ref: 'Supplier/S-5', title: 'Tamura', depth: 2 },
    { ref: 'Contract/C-9', title: 'Far', depth: 2 },
  ],
  links: {
    supplied_by: [
      { from: 'Part/P-1', to: 'Supplier/S-1' },
      { from: 'Part/P-1', to: 'Supplier/S-5' },
    ],
    feeds: [{ from: 'Supplier/S-1', to: 'Plant/TRN' }],
  },
  budget: {
    limit_tokens: 1000,
    used_tokens: 800,
    chars_per_token: 4,
    sections: { seeds: 200, neighbors: 400, links: 200 },
    trimmed: [{ step: 'neighbor_values', removed: 3 }],
    omitted: { neighbors: 2, links: 0 },
    over_budget: false,
    capped: false,
  },
};

describe('explainPack', () => {
  const entries = explainPack(pack, 'brevik');
  const why = (ref: string) => entries.find((e) => e.ref === ref)?.why;

  it('says a seed matched the search', () => {
    expect(entries[0]).toMatchObject({
      role: 'seed',
      depth: 0,
      why: 'Matched the search "brevik"',
    });
  });

  it('derives how a neighbor was reached from the Links of the pack', () => {
    expect(why('Part/P-1')).toBe('Depth 1, reached via inverse of supplied_by from Supplier/S-1');
    expect(why('Plant/TRN')).toBe('Depth 1, reached via feeds from Supplier/S-1');
    expect(why('Supplier/S-5')).toBe('Depth 2, reached via supplied_by from Part/P-1');
  });

  it('keeps a neighbor whose Link was trimmed, and notes trimmed values', () => {
    expect(why('Contract/C-9')).toBe('Depth 2, its Link is not in the pack');
    expect(entries.find((e) => e.ref === 'Plant/TRN')?.valuesTrimmed).toBe(true);
    expect(entries.find((e) => e.ref === 'Part/P-1')?.valuesTrimmed).toBe(false);
  });
});

describe('budgetReport', () => {
  it('reports usage per section and what the budget cost', () => {
    const report = budgetReport(pack.budget);
    expect(report).toMatchObject({ used: 800, limit: 1000, usage: 0.8, overBudget: false });
    expect(report.sections.map((s) => [s.name, s.share])).toEqual([
      ['seeds', 0.25],
      ['neighbors', 0.5],
      ['links', 0.25],
    ]);
    expect(report.trimmed).toEqual(['neighbor_values: 3 removed']);
    expect(report.omitted).toEqual(['neighbors: 2 left out']);
  });
});

describe('lab graph', () => {
  it('gathers the Records of both result lists and the pack once', () => {
    const refs = involvedRefs([hit('Part/P-1'), hit('Part/X')], [hit('Part/P-1')], pack);
    expect(refs).toEqual([
      'Part/P-1',
      'Part/X',
      'Supplier/S-1',
      'Plant/TRN',
      'Supplier/S-5',
      'Contract/C-9',
    ]);
    expect(involvedRefs(undefined, undefined, undefined)).toEqual([]);
  });

  it('highlights the involved nodes of a graph and the edges between them', () => {
    const data = {
      nodes: ['A/1', 'A/2', 'A/3'].map((id) => ({ id, label: id, group: 'A', color: '#000' })),
      edges: [
        { id: 'e0', source: 'A/1', target: 'A/2', label: 'r' },
        { id: 'e1', source: 'A/2', target: 'A/3', label: 'r' },
      ],
    };
    const highlight = labHighlight(data, ['A/1', 'A/2']);
    expect([...(highlight?.nodes ?? [])]).toEqual(['A/1', 'A/2']);
    expect([...(highlight?.edges ?? [])]).toEqual(['e0']);
    expect(labHighlight(data, ['B/9'])).toBeUndefined();
  });
});

describe('toRequest', () => {
  it('sends only the knobs that are set, and the default limit otherwise', () => {
    const params = {
      ...defaultParams(),
      mode: 'hybrid' as const,
      rrfK: '30',
      lexicalWeight: '0.5',
    };
    const { request, errors } = toRequest(params, 'q', [0.1, 0.2], 20);
    expect(request).toEqual({
      q: 'q',
      mode: 'hybrid',
      limit: 20,
      tuning: { rrf_k: 30, lexical_weight: 0.5 },
      query_vector: [0.1, 0.2],
    });
    expect(errors).toEqual({});
  });

  it('leaves out the vector in lexical mode and flags unusable numbers', () => {
    const params = { ...defaultParams(), candidates: '2.5', limit: '5', kinds: ['Part'] };
    const { request, errors } = toRequest(params, 'q', [1], 20);
    expect(request).toEqual({ q: 'q', mode: 'lexical', limit: 5, kinds: ['Part'] });
    expect(errors.candidates).toMatch(/whole number/);
  });
});

describe('guide client', () => {
  it('reads a pasted vector', () => {
    expect(parseVector('')).toEqual({ vector: null, error: null });
    expect(parseVector('[0.1, -2]').vector).toEqual([0.1, -2]);
    expect(parseVector('[0.1').error).toMatch(/valid JSON/);
    expect(parseVector('["a"]').error).toMatch(/array of numbers/);
    expect(parseVector('[]').vector).toBeNull();
  });

  it('validates the answer of /embed', () => {
    expect(readEmbedAnswer({ vector: [1, 2], model: 'm', dims: 2 })).toEqual({
      vector: [1, 2],
      model: 'm',
      dims: 2,
    });
    expect(() => readEmbedAnswer({ vector: 'nope' })).toThrow(/array of numbers/);
    expect(() => readEmbedAnswer(null)).toThrow();
  });
});
