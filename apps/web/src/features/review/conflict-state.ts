import type { Conflict, MergeAttempt, Resolution } from '@/api/types';

/** The stored Resolution of a Conflict, if any. */
export function resolutionOf(attempt: MergeAttempt, conflict: Conflict): Resolution | undefined {
  return attempt.resolutions.find((r) => r.fingerprint === conflict.fingerprint);
}

export function unresolvedOf(attempt: MergeAttempt): Conflict[] {
  const resolved = new Set(attempt.resolutions.map((r) => r.fingerprint));
  return attempt.conflicts.filter((c) => !resolved.has(c.fingerprint));
}
