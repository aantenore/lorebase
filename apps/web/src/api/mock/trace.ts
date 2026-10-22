import type { KeeperApi } from '../client';
import { notFound } from '../errors';
import type { TraceSession, TraceStep } from '../types';
import { decodeCursor, encodeCursor } from './engine';
import { buildTraceSeed, type TraceSeed } from './sample/trace';

const unique = (values: (string | null | undefined)[]) => [
  ...new Set(values.filter((v): v is string => typeof v === 'string' && v !== '')),
];

/** The listing entry of a session, derived from its steps. */
function summarize(sessionId: string, steps: TraceStep[]): TraceSession {
  const times = steps.map((s) => s.at).sort();
  const calls = steps.filter((s) => s.type === 'call');
  return {
    session_id: sessionId,
    actor: steps[0]?.actor ?? 'unknown',
    started_at: times[0] ?? '',
    last_at: times[times.length - 1] ?? '',
    calls: calls.length,
    notes: steps.length - calls.length,
    domains: unique(steps.map((s) => s.domain_id)),
    tools: unique(calls.map((s) => s.tool)),
    proposals: unique(steps.map((s) => s.proposal_id)),
    inferred: steps.some((s) => s.inferred),
  };
}

/** An in-memory trace: the sessions of the sample installation, newest activity first. */
export function createTraceMock(seed: TraceSeed = buildTraceSeed()): KeeperApi['trace'] {
  const stepsOf = (sessionId: string, actor: string | undefined) => {
    const steps = seed.sessions[sessionId];
    if (steps === undefined || (actor !== undefined && steps[0]?.actor !== actor)) {
      throw notFound(`Session ${sessionId}`);
    }
    return steps;
  };

  return {
    sessions: (params) => {
      const all = Object.entries(seed.sessions)
        .map(([id, steps]) => summarize(id, steps))
        .filter((s) => params?.actor === undefined || s.actor === params.actor)
        .filter((s) => params?.domain_id === undefined || s.domains.includes(params.domain_id))
        .sort((a, b) => b.last_at.localeCompare(a.last_at));
      const offset = decodeCursor(params?.cursor);
      const limit = params?.limit;
      const items = limit === undefined ? all.slice(offset) : all.slice(offset, offset + limit);
      const more = limit !== undefined && offset + limit < all.length;
      return Promise.resolve({
        enabled: true,
        items,
        next_cursor: more ? encodeCursor(offset + limit) : null,
      });
    },
    timeline: (sessionId, params) =>
      Promise.resolve({
        session_id: sessionId,
        steps: structuredClone(stepsOf(sessionId, params?.actor)),
        truncated: false,
      }),
  };
}
