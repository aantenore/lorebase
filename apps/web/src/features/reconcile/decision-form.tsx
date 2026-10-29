import { useState } from 'react';
import type {
  ReconciliationCandidate,
  ReconciliationChoice,
  ReconciliationPolicy,
  ResolveCandidateInput,
} from '@/api/reconciliation-types';
import type { JsonValue } from '@/api/types';
import { validateValue, type ValueSpec } from '@/api/values';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Button } from '@/ui/button';
import { Select } from '@/ui/field';
import { ValueInput } from '@/ui/value-input';
import { ReasonField, type Decide } from './decide';
import { showValue, sourceLabel } from './reconcile-state';

const text = reconcileMessages;

type ValueChoice = Exclude<ReconciliationChoice, 'merge'>;
const CHOICES: readonly ValueChoice[] = ['claim', 'keep_current', 'custom', 'policy'];
const POLICIES: readonly ReconciliationPolicy[] = [
  'authority',
  'newest_wins',
  'prefer_current',
  'prefer_incoming',
];

/**
 * The decision on a value candidate: a claim, the current value, a custom value or a policy,
 * always with a reason. Preview asks Keeper what the decision would write (`dry_run`); Dismiss
 * takes the same reason.
 */
export function DecisionForm({
  decide,
  candidate,
  spec,
  sourceNames,
  currentClaimId,
}: {
  decide: Decide;
  candidate: ReconciliationCandidate;
  /** What a custom value must be; `null` accepts any JSON. */
  spec: ValueSpec | null;
  sourceNames: ReadonlyMap<string, string>;
  currentClaimId: string | undefined;
}) {
  const [choice, setChoice] = useState<ValueChoice>('claim');
  const [claimId, setClaimId] = useState(
    () => candidate.claims.find((c) => c.claim_id !== currentClaimId)?.claim_id ?? '',
  );
  const [custom, setCustom] = useState<{ value: JsonValue | undefined; error: string | null }>({
    value: undefined,
    error: null,
  });
  const [policy, setPolicy] = useState<ReconciliationPolicy>('authority');
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);

  const customProblem =
    custom.error ??
    (custom.value === undefined || custom.value === null
      ? text.decide.enterValue
      : spec === null
        ? null
        : validateValue(spec, custom.value));
  const choiceProblem =
    choice === 'claim' && claimId === ''
      ? text.decide.pickClaim
      : choice === 'custom'
        ? customProblem
        : null;

  const input = (dryRun: boolean): ResolveCandidateInput | null => {
    setAttempted(true);
    if (reason.trim() === '' || choiceProblem !== null) return null;
    return {
      choice,
      reason: reason.trim(),
      dry_run: dryRun,
      ...(choice === 'claim' ? { claim_id: claimId } : {}),
      ...(choice === 'custom' && custom.value !== undefined ? { value: custom.value } : {}),
      ...(choice === 'policy' ? { policy } : {}),
    };
  };
  const submit = (dryRun: boolean) => {
    const built = input(dryRun);
    if (built !== null) decide.mutate({ kind: 'resolve', input: built });
  };

  return (
    <form
      aria-label={text.decide.title}
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-muted">{text.decide.legend}</legend>
        {CHOICES.map((option) => (
          <label key={option} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`choice-${candidate.id}`}
              checked={choice === option}
              onChange={() => {
                setChoice(option);
              }}
            />
            {text.decide.choices[option]}
          </label>
        ))}
      </fieldset>

      {choice === 'claim' && (
        <Select
          label={text.decide.claim}
          value={claimId}
          error={attempted && claimId === '' ? text.decide.pickClaim : undefined}
          options={[
            ...(claimId === '' ? [{ value: '', label: '-' }] : []),
            ...candidate.claims.map((claim, index) => ({
              value: claim.claim_id,
              label: text.decide.claimOption(
                index + 1,
                showValue(claim.value),
                sourceLabel(claim.source, sourceNames),
              ),
            })),
          ]}
          onChange={(event) => {
            setClaimId(event.target.value);
          }}
        />
      )}
      {choice === 'custom' && (
        <ValueInput
          label={text.decide.custom}
          spec={spec}
          value={custom.value}
          error={attempted ? customProblem : null}
          onChange={(value, parseError) => {
            setCustom({ value, error: parseError });
          }}
        />
      )}
      {choice === 'policy' && (
        <Select
          label={text.decide.policy}
          value={policy}
          options={POLICIES.map((value) => ({ value, label: text.decide.policies[value] }))}
          onChange={(event) => {
            setPolicy(POLICIES.find((p) => p === event.target.value) ?? 'authority');
          }}
        />
      )}

      <ReasonField value={reason} onChange={setReason} showError={attempted} />

      <div className="flex flex-wrap gap-2">
        <Button
          disabled={decide.isPending}
          onClick={() => {
            submit(true);
          }}
        >
          {text.decide.preview}
        </Button>
        <Button type="submit" variant="primary" disabled={decide.isPending}>
          {text.decide.apply}
        </Button>
        <Button
          variant="ghost"
          disabled={decide.isPending}
          onClick={() => {
            setAttempted(true);
            if (reason.trim() !== '') decide.mutate({ kind: 'dismiss', reason: reason.trim() });
          }}
        >
          {text.decide.dismiss}
        </Button>
      </div>
    </form>
  );
}

/** Reopens a dismissed candidate, with a reason. */
export function ReopenForm({ decide }: { decide: Decide }) {
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  return (
    <form
      aria-label={text.decide.reopenTitle}
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        setAttempted(true);
        if (reason.trim() !== '') decide.mutate({ kind: 'reopen', reason: reason.trim() });
      }}
    >
      <p className="text-sm text-muted">{text.decide.reopenHint}</p>
      <ReasonField value={reason} onChange={setReason} showError={attempted} />
      <div>
        <Button type="submit" disabled={decide.isPending}>
          {text.decide.reopen}
        </Button>
      </div>
    </form>
  );
}
