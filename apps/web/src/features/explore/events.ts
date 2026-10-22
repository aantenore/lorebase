import type { SseMessage } from './sse';

/**
 * The events of `POST {guideUrl}/explore` (docs/guide.md). Every text in them is model output
 * that may quote graph data: the panel renders it as plain text only. Only `citations` become
 * links, and only after `safeCitation` accepted them.
 */
export interface Citation {
  ref: string;
  kind: string;
  key: string;
}

export type ExploreEvent =
  | { type: 'session'; session_id: string; domain: string; branch: string | null; model?: string }
  | { type: 'thought'; step: number; text: string }
  | { type: 'tool_call'; step: number; tool: string; args: Record<string, unknown> }
  | {
      type: 'tool_result';
      step: number;
      tool: string;
      ok: boolean;
      summary: string;
      refs: string[];
    }
  | { type: 'error'; code: string; message: string }
  | { type: 'answer'; text: string; citations: Citation[]; found: boolean }
  | {
      type: 'done';
      session_id: string;
      steps: number;
      outcome: string;
      tokens: { input: number; output: number };
      elapsed_ms: number;
      model?: string;
    };

/** Longest text kept from one field; the rest is cut so a runaway answer cannot flood the page. */
const MAX_TEXT = 20_000;
const KIND = /^[A-Z][A-Za-z0-9]{0,127}$/;
const MAX_KEY = 512;
// Control characters have no place in a Record key; a key holding one is not linked.
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

const str = (value: unknown, fallback = ''): string =>
  typeof value === 'string' ? value.slice(0, MAX_TEXT) : fallback;
const num = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;
const obj = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const optionalModel = (value: unknown): { model?: string } =>
  typeof value === 'string' && value !== '' ? { model: value.slice(0, 200) } : {};

/**
 * A citation the app may turn into a link to a Record page: a well-formed Kind name, a key
 * without control characters and not a path step, and a `ref` that is exactly `kind/key`.
 * Anything else is `null` and is not linked.
 */
export function safeCitation(raw: unknown): Citation | null {
  const value = obj(raw);
  const kind = value['kind'];
  const key = value['key'];
  const ref = value['ref'];
  if (typeof kind !== 'string' || typeof key !== 'string') return null;
  if (!KIND.test(kind)) return null;
  if (key === '' || key.length > MAX_KEY || CONTROL.test(key) || key === '.' || key === '..') {
    return null;
  }
  if (ref !== undefined && ref !== `${kind}/${key}`) return null;
  return { ref: `${kind}/${key}`, kind, key };
}

/** Validates one server-sent event; `null` for an unknown event or data that is not JSON. */
export function parseExploreEvent(message: SseMessage): ExploreEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(message.data);
  } catch {
    return null;
  }
  const data = obj(parsed);
  switch (message.event) {
    case 'session':
      return {
        type: 'session',
        session_id: str(data['session_id']),
        domain: str(data['domain']),
        branch: typeof data['branch'] === 'string' ? data['branch'] : null,
        ...optionalModel(data['model']),
      };
    case 'thought':
      return { type: 'thought', step: num(data['step']), text: str(data['text']) };
    case 'tool_call':
      return {
        type: 'tool_call',
        step: num(data['step']),
        tool: str(data['tool']),
        args: obj(data['args']),
      };
    case 'tool_result': {
      const refs = Array.isArray(data['refs']) ? data['refs'] : [];
      return {
        type: 'tool_result',
        step: num(data['step']),
        tool: str(data['tool']),
        ok: data['ok'] === true,
        summary: str(data['summary']),
        refs: refs.filter((r): r is string => typeof r === 'string'),
      };
    }
    case 'error':
      return { type: 'error', code: str(data['code'], 'error'), message: str(data['message']) };
    case 'answer': {
      const raw = Array.isArray(data['citations']) ? data['citations'] : [];
      return {
        type: 'answer',
        text: str(data['text']),
        citations: raw.map(safeCitation).filter((c): c is Citation => c !== null),
        found: data['found'] === true,
      };
    }
    case 'done': {
      const tokens = obj(data['tokens']);
      return {
        type: 'done',
        session_id: str(data['session_id']),
        steps: num(data['steps']),
        outcome: str(data['outcome']),
        tokens: { input: num(tokens['input']), output: num(tokens['output']) },
        elapsed_ms: num(data['elapsed_ms']),
        ...optionalModel(data['model']),
      };
    }
    default:
      return null;
  }
}

/** Tool arguments on one line, cut to `max` characters. */
export function compactArgs(args: Record<string, unknown>, max = 160): string {
  const text = JSON.stringify(args);
  return text.length <= max ? text : `${text.slice(0, max - 3)}...`;
}
