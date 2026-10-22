import type { TraceStep } from '@/api/types';
import { edgeId } from '@/features/canvas/ref-graph';
import { clockTime, countsSummary, parseResult, queryText } from './step-view';
import { stepRefs, traceGraph } from './trace-graph';

const step = (n: number, refs: TraceStep['refs'], type: TraceStep['type'] = 'call'): TraceStep => ({
  step: n,
  seq: n,
  type,
  at: '2026-10-20T09:14:02Z',
  actor: 'svc_herald',
  inferred: false,
  withheld: false,
  refs,
});

const rec = (kind: string, key: string) => ({ kind, key });

const steps: TraceStep[] = [
  step(1, { records: [rec('Supplier', 'S-1'), rec('Part', 'P-1')], links: [], truncated: false }),
  step(2, null, 'note'),
  step(3, {
    records: [rec('Part', 'P-1'), rec('Plant', 'TRN')],
    links: [{ relation: 'supplied_by', from: rec('Part', 'P-1'), to: rec('Supplier', 'S-1') }],
    truncated: false,
  }),
];

describe('traceGraph', () => {
  it('reads the refs of a step as Kind/key references', () => {
    expect(stepRefs(steps[0] as TraceStep).records).toEqual(['Supplier/S-1', 'Part/P-1']);
    expect(stepRefs(steps[1] as TraceStep)).toEqual({ records: [], links: [] });
    expect(stepRefs(steps[2] as TraceStep).links).toEqual([
      { relation: 'supplied_by', from: 'Part/P-1', to: 'Supplier/S-1' },
    ]);
  });

  it('unites the Records and Links of steps 1 to the selected one', () => {
    const upTo1 = traceGraph(steps, 1, []);
    expect(upTo1.data.nodes.map((n) => n.id)).toEqual(['Supplier/S-1', 'Part/P-1']);
    expect(upTo1.data.edges).toEqual([]);

    const upTo3 = traceGraph(steps, 3, []);
    expect(upTo3.data.nodes.map((n) => n.id)).toEqual(['Supplier/S-1', 'Part/P-1', 'Plant/TRN']);
    expect(upTo3.data.edges).toEqual([
      {
        id: 'supplied_by|Part/P-1|Supplier/S-1',
        source: 'Part/P-1',
        target: 'Supplier/S-1',
        label: 'supplied_by',
      },
    ]);
  });

  it('highlights what the selected step touched, Link ends included', () => {
    const { highlight } = traceGraph(steps, 3, []);
    expect([...(highlight?.nodes ?? [])].sort()).toEqual(['Part/P-1', 'Plant/TRN', 'Supplier/S-1']);
    expect([...(highlight?.edges ?? [])]).toEqual([
      edgeId({ relation: 'supplied_by', from: 'Part/P-1', to: 'Supplier/S-1' }),
    ]);
  });

  it('highlights nothing for a step that touched nothing, so nothing is dimmed', () => {
    const { data, highlight } = traceGraph(steps, 2, []);
    expect(highlight).toBeUndefined();
    expect(data.nodes).toHaveLength(2);
  });

  it('draws nothing for a session without refs', () => {
    expect(traceGraph([step(1, null)], 1, []).data).toEqual({ nodes: [], edges: [] });
  });
});

describe('step view', () => {
  it('reads counts, mode, corrections and budget of a result', () => {
    const view = parseResult({
      counts: { items: 3, hops: 2, bad: 'x' },
      mode: 'hybrid',
      corrections: [{ word: 'beerings', replacements: ['bearings'] }, { nope: 1 }],
      budget: { limit_tokens: 1200, used_tokens: 1042, over_budget: false, capped: true },
      truncated: true,
    });
    expect(countsSummary(view.counts)).toBe('items 3, hops 2');
    expect(view.mode).toBe('hybrid');
    expect(view.corrections).toEqual([{ word: 'beerings', replacements: ['bearings'] }]);
    expect(view.budget).toEqual({
      limitTokens: 1200,
      usedTokens: 1042,
      overBudget: false,
      capped: true,
    });
    expect(view.truncated).toBe(true);
    expect(view.hasMore).toBe(false);
  });

  it('shows less, not an error, for an unknown result shape', () => {
    expect(parseResult(null)).toMatchObject({ counts: [], mode: null, budget: null });
    expect(parseResult({ counts: 'many', budget: { limit_tokens: 'x' } }).budget).toBeNull();
  });

  it('formats the query as JSON text and the clock time', () => {
    expect(queryText(null)).toBeNull();
    expect(queryText({})).toBeNull();
    expect(queryText({ body: { q: { redacted: 'string' } } })).toContain('"redacted": "string"');
    expect(clockTime('2026-10-20T09:14:02Z')).toBe('09:14:02');
    expect(clockTime('soon')).toBe('soon');
  });
});
