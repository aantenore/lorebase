import type { Citation, ExploreEvent } from './events';
import type { ExploreRequest, ExploreTransport } from './explore-client';
import { PRESETS } from './presets';

/**
 * Scripted explorations for the mock API (development and screenshots): the events Guide would
 * stream for the preset questions over the sample `supply` Domain. Any other question gets a
 * short search that finds nothing. No model and no network are involved.
 */

/** A session of the mock trace, so "View trace" opens a timeline in mock mode too. */
const MOCK_SESSION = 'sess_20261020_brevik';
const MOCK_MODEL = 'scripted (mock)';

type Step =
  | { thought: string }
  | { tool: string; args: Record<string, unknown>; summary: string; refs?: string[]; ok?: boolean };

interface Script {
  steps: Step[];
  answer: string | null;
  cite?: string[];
}

const cite = (ref: string): Citation => {
  const index = ref.indexOf('/');
  return { ref, kind: ref.slice(0, index), key: ref.slice(index + 1) };
};

const SCRIPTS: Readonly<Record<string, Script>> = {
  'single-source': {
    steps: [
      { thought: 'Part.single_sourced marks parts with one supplier; query those first.' },
      {
        tool: 'query',
        args: { kind: 'Part', where: { derived: 'single_sourced', op: 'eq', value: true } },
        summary: '7 records',
        refs: ['Part/P-55120', 'Part/P-90310', 'Part/P-90311', 'Part/P-77813', 'Part/P-10033'],
      },
      { thought: 'Follow supplied_by to the suppliers and compare their risk scores.' },
      {
        tool: 'traverse',
        args: { start: 'Part/P-55120', path: ['supplied_by'] },
        summary: '1 hops, 1 Records',
        refs: ['Part/P-55120', 'Supplier/S-1003'],
      },
      {
        tool: 'neighborhood',
        args: { refs: ['Part/P-90310', 'Part/P-77813'], depth: 1 },
        summary: '6 nodes, 5 edges',
        refs: ['Part/P-90310', 'Supplier/S-1004', 'Part/P-77813', 'Supplier/S-1001'],
      },
    ],
    answer:
      'Seven parts have a single supplier. The riskiest dependency is P-55120 (Gearbox housing) on Kestrel Castings Ltd (S-1003, risk score 71). Next come P-90310 and P-90311 on Orlova Seals (S-1004, risk 48) and P-77813 on Brevik Bearings (S-1001, risk 35).',
    cite: [
      'Part/P-55120',
      'Supplier/S-1003',
      'Part/P-90310',
      'Supplier/S-1004',
      'Part/P-77813',
      'Supplier/S-1001',
    ],
  },
  'tier1-risk': {
    steps: [
      { thought: 'Look for tier-1 suppliers with a high risk score.' },
      {
        tool: 'query',
        args: { kind: 'Supplier', where: { attr: 'tier', op: 'eq', value: '1' } },
        summary: '2 records',
        refs: ['Supplier/S-1002', 'Supplier/S-1005'],
      },
      { thought: 'Both score 20 or less, and the Domain has no purchase orders to check.' },
      { tool: 'schema', args: {}, summary: '6 Kinds, 4 Relations' },
    ],
    answer: null,
  },
  'what-if': {
    steps: [
      { thought: 'Read Supplier S-1007 and the parts it supplies.' },
      {
        tool: 'get',
        args: { ref: 'Supplier/S-1007' },
        summary: 'Keeper error 404',
        ok: false,
      },
      {
        tool: 'search',
        args: { q: 'S-1007', mode: 'lexical' },
        summary: '0 hits (lexical)',
      },
    ],
    answer: null,
  },
  history: {
    steps: [
      { thought: 'Read the contract, then its change history.' },
      {
        tool: 'get',
        args: { ref: 'Contract/C-3013' },
        summary: 'Contract/C-3013',
        refs: ['Contract/C-3013'],
      },
      {
        tool: 'history',
        args: { ref: 'Contract/C-3013' },
        summary: '2 changes of Contract/C-3013',
        refs: ['Contract/C-3013'],
      },
      {
        tool: 'links',
        args: { ref: 'Contract/C-3013', relation: 'with_supplier' },
        summary: '1 links of Contract/C-3013',
        refs: ['Supplier/S-1013'],
      },
    ],
    answer:
      'C-3013 is the framework supply agreement with Wexrix Filtration (S-1013). It was created from the ERP import and changed once since, when Amendment 2 was extracted from its document.',
    cite: ['Contract/C-3013', 'Supplier/S-1013'],
  },
  typo: {
    steps: [
      { thought: '"bering" is likely a typo of bearing; lexical search corrects close words.' },
      {
        tool: 'search',
        args: { q: 'bering', mode: 'lexical', limit: 10 },
        summary: '3 hits (lexical)',
        refs: ['Part/P-77812', 'Part/P-77813', 'Part/P-10033'],
      },
      {
        tool: 'neighborhood',
        args: { refs: ['Part/P-77812', 'Part/P-77813', 'Part/P-10033'], depth: 1 },
        summary: '6 nodes, 4 edges',
        refs: ['Supplier/S-1001', 'Supplier/S-1005', 'Supplier/S-1025'],
      },
    ],
    answer:
      'Bearings come from Brevik Bearings (S-1001: P-77812 and P-77813), Tamura Precision (S-1005, second source of P-77812) and Kavvik Filtration (S-1025: P-10033).',
    cite: [
      'Supplier/S-1001',
      'Part/P-77812',
      'Part/P-77813',
      'Supplier/S-1005',
      'Supplier/S-1025',
      'Part/P-10033',
    ],
  },
};

const FALLBACK: Script = {
  steps: [
    { thought: 'Search the Domain for the words of the question.' },
    { tool: 'search', args: { mode: 'lexical' }, summary: '0 hits (lexical)' },
  ],
  answer: null,
};

function scriptFor(request: ExploreRequest): Script {
  const preset = (PRESETS[request.domain] ?? []).find((p) => p.question === request.question);
  return (preset === undefined ? undefined : SCRIPTS[preset.id]) ?? FALLBACK;
}

/** The events of a script, cut to `max_steps` tool calls like Guide does. */
export function scriptedEvents(request: ExploreRequest): ExploreEvent[] {
  const script = scriptFor(request);
  const events: ExploreEvent[] = [
    {
      type: 'session',
      session_id: MOCK_SESSION,
      domain: request.domain,
      branch: request.branch,
      model: MOCK_MODEL,
    },
  ];
  const maxSteps = request.max_steps ?? Number.POSITIVE_INFINITY;
  let step = 0;
  for (const item of script.steps) {
    if ('thought' in item) {
      events.push({ type: 'thought', step, text: item.thought });
      continue;
    }
    if (step >= maxSteps) break;
    step += 1;
    events.push({
      type: 'tool_call',
      step,
      tool: item.tool,
      args: {
        ...item.args,
        ...(item.tool === 'search' && !('q' in item.args) ? { q: request.question } : {}),
      },
    });
    events.push({
      type: 'tool_result',
      step,
      tool: item.tool,
      ok: item.ok ?? true,
      summary: item.summary,
      refs: item.refs ?? [],
    });
  }
  const found = script.answer !== null;
  events.push({
    type: 'answer',
    text: script.answer ?? 'Not found in the graph.',
    citations: found ? (script.cite ?? []).map(cite) : [],
    found,
  });
  events.push({
    type: 'done',
    session_id: MOCK_SESSION,
    steps: step,
    outcome: found ? 'answered' : 'not_found',
    tokens: { input: 1800 * step + 900, output: 160 * step + 120 },
    elapsed_ms: 900 * step + 600,
    model: MOCK_MODEL,
  });
  return events;
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Plays the script with `stepMs` between two events. */
export function mockTransport(stepMs: number): ExploreTransport {
  return async function* explore(request, signal) {
    for (const event of scriptedEvents(request)) {
      await pause(stepMs, signal);
      yield event;
    }
  };
}
