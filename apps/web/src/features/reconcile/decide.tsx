import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { ConflictError } from '@/api/errors';
import { useKeeperApi } from '@/api/provider';
import type {
  ReconciliationCandidate,
  ReconciliationDecision,
  ResolveCandidateInput,
} from '@/api/reconciliation-types';
import { useScopeSearch } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Textarea } from '@/ui/field';
import { showValue } from './reconcile-state';
import { decisionError } from './status';

const text = reconcileMessages;

type Action =
  | { kind: 'resolve'; input: ResolveCandidateInput }
  | { kind: 'dismiss'; reason: string }
  | { kind: 'reopen'; reason: string };

/**
 * Decisions on one candidate: resolve (or preview with `dry_run`), dismiss, reopen. A decision
 * that writes reads everything again (values, Proposals, Branches); a refused one shows on the
 * screen, and a 409 reloads the candidate as it now is.
 */
export function useDecide(domain: string, candidate: string) {
  const api = useKeeperApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (action: Action): Promise<ReconciliationDecision> => {
      if (action.kind === 'resolve') {
        return api.reconciliation.resolve(domain, candidate, action.input);
      }
      if (action.kind === 'dismiss') {
        return api.reconciliation.dismiss(domain, candidate, action.reason);
      }
      return api.reconciliation.reopen(domain, candidate, action.reason);
    },
    meta: { inlineError: true },
    onSuccess: async (decision) => {
      if (decision.status === 'dry_run' || decision.status === 'undecided') return;
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
    },
    onError: async (error) => {
      if (error instanceof ConflictError) {
        await queryClient.invalidateQueries({ queryKey: ['keeper', 'reconciliation', domain] });
      }
    },
  });
}

export type Decide = ReturnType<typeof useDecide>;

/** The reason every decision needs, with its error once a decision was attempted without one. */
export function ReasonField({
  value,
  onChange,
  showError,
}: {
  value: string;
  onChange: (value: string) => void;
  showError: boolean;
}) {
  return (
    <Textarea
      label={text.decide.reason}
      hint={text.decide.reasonHint}
      error={showError && value.trim() === '' ? text.decide.reasonRequired : undefined}
      rows={2}
      value={value}
      aria-required
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}

function ProposalLink({ domain, proposal }: { domain: string; proposal: string }) {
  const scopeSearch = useScopeSearch();
  return (
    <Link
      to="/$domain/review/$proposal"
      params={{ domain, proposal }}
      search={scopeSearch}
      className="text-accent hover:underline"
    >
      {text.outcome.openProposal(proposal)}
    </Link>
  );
}

/** What the last decision did, or what a preview says it would do; errors as an alert. */
export function DecisionOutcome({
  domain,
  candidate,
  decision,
  error,
}: {
  domain: string;
  candidate: ReconciliationCandidate;
  decision: ReconciliationDecision | undefined;
  error: unknown;
}) {
  if (error !== null && error !== undefined) {
    return (
      <p role="alert" className="rounded-md border border-danger p-2 text-sm text-danger">
        {decisionError(error)}
      </p>
    );
  }
  if (decision === undefined) return null;
  if (decision.status === 'dry_run') {
    const index = candidate.claims.findIndex((c) => c.claim_id === decision.claim_id);
    return (
      <div
        role="status"
        aria-label={text.decide.previewTitle}
        className="rounded-md border border-line bg-surface-2 p-2 text-sm"
      >
        {index >= 0 && <p>{text.decide.previewClaim(index + 1)}</p>}
        {decision.edits.length === 0 ? (
          <p>{decision.detail ?? text.decide.previewNothing}</p>
        ) : (
          <>
            <p>{text.decide.previewWrites(decision.edits.length)}</p>
            <ul className="list-disc pl-5 font-mono text-xs">
              {decision.edits.map((edit, at) => (
                <li key={at} className="break-words">
                  {Object.entries(edit)
                    .map(([key, value]) => `${key}: ${showValue(value)}`)
                    .join(', ')}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    );
  }
  const message =
    decision.status === 'undecided'
      ? `${text.outcome.undecided} ${decision.detail ?? ''}`.trim()
      : decision.status === 'proposed'
        ? text.outcome.proposed
        : text.outcome[decision.status];
  return (
    <p
      role="status"
      className={
        decision.status === 'undecided'
          ? 'rounded-md border border-warning p-2 text-sm'
          : 'rounded-md border border-success p-2 text-sm'
      }
    >
      {message}
      {decision.proposal !== null && (
        <>
          {' '}
          <ProposalLink domain={domain} proposal={decision.proposal} />
        </>
      )}
      {decision.proposal === null && decision.revision !== null && (
        <span className="text-muted"> {text.outcome.revision(decision.revision)}</span>
      )}
    </p>
  );
}

export { ProposalLink };
