import type { Citation, ExploreEvent } from '@/features/explore/events';
import type { ExploreRequest, ExploreTransport } from '@/features/explore/explore-client';
import { SUPPLY_SCRIPTS } from './sample/explore';

/**
 * Scripted explorations for the mock API (development and screenshots): the events Guide would
 * stream for the sample questions of a mock Domain. Any other question gets a short search that
 * finds nothing. No model and no network are involved.
 */

/** A session of the mock trace, so "View trace" opens a timeline in mock mode too. */
const MOCK_SESSION = 'sess_20261020_brevik';
const MOCK_MODEL = 'scripted (mock)';

type Step =
  | { thought: string }
  | { tool: string; args: Record<string, unknown>; summary: string; refs?: string[]; ok?: boolean };

export interface ExploreScript {
  steps: Step[];
  answer: string | null;
  cite?: string[];
}

const cite = (ref: string): Citation => {
  const index = ref.indexOf('/');
  return { ref, kind: ref.slice(0, index), key: ref.slice(index + 1) };
};

const FALLBACK: ExploreScript = {
  steps: [
    { thought: 'Search the Domain for the words of the question.' },
    { tool: 'search', args: { mode: 'lexical' }, summary: '0 hits (lexical)' },
  ],
  answer: null,
};

/** Scripts per mock Domain id, by question text. */
const SCRIPTS: Readonly<Record<string, Readonly<Record<string, ExploreScript>>>> = {
  supply: SUPPLY_SCRIPTS,
};

function scriptFor(request: ExploreRequest): ExploreScript {
  const scripts = Object.hasOwn(SCRIPTS, request.domain) ? SCRIPTS[request.domain] : undefined;
  const script =
    scripts !== undefined && Object.hasOwn(scripts, request.question)
      ? scripts[request.question]
      : undefined;
  return script ?? FALLBACK;
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
