import type { CandidateStatus } from '@/api/reconciliation-types';
import { ConflictError, ForbiddenError, describeError } from '@/api/errors';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import type { BadgeTone } from '@/ui/layout';

export const STATUS_TONE: Record<CandidateStatus, BadgeTone> = {
  open: 'accent',
  resolved: 'success',
  dismissed: 'neutral',
  obsolete: 'neutral',
};

/** Problem codes of decisions that have a sentence of their own. */
const CODES: Readonly<Record<string, string>> = {
  'candidate-not-open': reconcileMessages.errors.notOpen,
  'reconciliation-open': reconcileMessages.errors.reconciliationOpen,
  'reconciliation-pending': reconcileMessages.errors.pending,
  'reconciliation-stale': reconcileMessages.errors.stale,
};

/**
 * One line for a refused decision: what a person can do about a 403 or a known 409, then
 * Keeper's own detail; any other failure as the app describes errors elsewhere.
 */
export function decisionError(error: unknown): string {
  if (error instanceof ForbiddenError) {
    return `${reconcileMessages.errors.forbidden} ${error.message}`.trim();
  }
  if (error instanceof ConflictError) {
    const known = error.code === undefined ? undefined : CODES[error.code];
    if (known !== undefined) return known;
  }
  return describeError(error);
}
