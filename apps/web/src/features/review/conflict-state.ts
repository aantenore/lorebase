import type {
  Conflict,
  ConflictCode,
  MergeAttempt,
  Resolution,
  ResolutionChoice,
} from '@/api/types';
import { locationOf, messageOf, type Names } from './conflict-meta';

/** The stored Resolution of a Conflict, if any. */
export function resolutionOf(attempt: MergeAttempt, conflict: Conflict): Resolution | undefined {
  return attempt.resolutions.find((r) => r.fingerprint === conflict.fingerprint);
}

export function unresolvedOf(attempt: MergeAttempt): Conflict[] {
  const resolved = new Set(attempt.resolutions.map((r) => r.fingerprint));
  return attempt.conflicts.filter((c) => !resolved.has(c.fingerprint));
}

export type StatusFilter = 'all' | 'open' | 'resolved';

export interface ConflictFilter {
  kind: ConflictCode | 'all';
  status: StatusFilter;
  text: string;
}

export const NO_FILTER: ConflictFilter = { kind: 'all', status: 'all', text: '' };

/** Conflicts of the attempt that pass the filter, in the order the attempt lists them. */
export function filterConflicts(
  attempt: MergeAttempt,
  filter: ConflictFilter,
  names: Names,
): Conflict[] {
  const resolved = new Set(attempt.resolutions.map((r) => r.fingerprint));
  const needle = filter.text.trim().toLowerCase();
  return attempt.conflicts.filter((conflict) => {
    if (filter.kind !== 'all' && conflict.kind !== filter.kind) return false;
    if (filter.status === 'open' && resolved.has(conflict.fingerprint)) return false;
    if (filter.status === 'resolved' && !resolved.has(conflict.fingerprint)) return false;
    if (needle === '') return true;
    const haystack = [locationOf(conflict, names), messageOf(conflict, names), ...conflict.sample];
    return haystack.some((text) => text.toLowerCase().includes(needle));
  });
}

/** How many stored Resolutions use each choice. */
export function countByChoice(attempt: MergeAttempt): [ResolutionChoice, number][] {
  const counts = new Map<ResolutionChoice, number>();
  for (const resolution of attempt.resolutions) {
    counts.set(resolution.choice, (counts.get(resolution.choice) ?? 0) + 1);
  }
  return [...counts];
}
