import { readText } from '@/app/scope';

/**
 * Search params of the Agent activity screen. A session's timeline is at
 * `/<domain>/agent-activity?session=<session_id>`; `actor` narrows the list and the timeline to
 * one principal, `step` is the step the Canvas overlay stops at (the last one when absent).
 */
export interface ActivitySearch {
  session?: string | undefined;
  actor?: string | undefined;
  step?: number | undefined;
}

/** A step number from the URL: a positive integer, or nothing. */
export function readStep(value: unknown): number | undefined {
  const text = readText(value);
  if (text === undefined) return undefined;
  const parsed = Number(text);
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : undefined;
}

export function validateActivitySearch(raw: Record<string, unknown>): ActivitySearch {
  return {
    session: readText(raw['session']),
    actor: readText(raw['actor']),
    step: readStep(raw['step']),
  };
}
