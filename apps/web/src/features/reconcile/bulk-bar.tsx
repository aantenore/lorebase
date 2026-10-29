import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import type {
  CandidateFilter,
  ReconciliationPolicy,
  ResolveCandidatesInput,
} from '@/api/reconciliation-types';
import { useScope } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { FormAction, FormRow } from '@/ui/layout';
import { decisionError } from './status';

const text = reconcileMessages;

type Scope = 'selected' | 'filter';
type BulkAction = ResolveCandidatesInput['choice'];
const POLICIES: readonly ReconciliationPolicy[] = [
  'authority',
  'newest_wins',
  'prefer_current',
  'prefer_incoming',
];

/**
 * One policy or a dismissal over the selected candidates, or over every open candidate of the
 * filters (Keeper caps the set at `bulk_max`). Each candidate is decided and audited on its own.
 */
export function BulkBar({
  selected,
  filter,
  selectedOnly = false,
  onDone,
}: {
  selected: string[];
  filter: Omit<CandidateFilter, 'status'>;
  /** Keeper's bulk filter cannot narrow by assignment: only a selection is offered. */
  selectedOnly?: boolean;
  onDone: () => void;
}) {
  const scope = useScope();
  const api = useKeeperApi();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<Scope>('selected');
  const [action, setAction] = useState<BulkAction>('policy');
  const [policy, setPolicy] = useState<ReconciliationPolicy>('authority');
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);
  const run = useMutation({
    mutationFn: (input: ResolveCandidatesInput) =>
      api.reconciliation.resolveMany(scope.domain, input),
    meta: { inlineError: true },
    onSuccess: async (_result, input) => {
      if (input.dry_run === true) return;
      onDone();
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
    },
  });

  const effective: Scope = selectedOnly ? 'selected' : target;
  const empty = effective === 'selected' && selected.length === 0;
  const submit = (dryRun: boolean) => {
    setAttempted(true);
    if (reason.trim() === '' || empty) return;
    run.mutate({
      ...(effective === 'selected' ? { ids: selected } : { filter }),
      choice: action,
      ...(action === 'policy' ? { policy } : {}),
      reason: reason.trim(),
      dry_run: dryRun,
    });
  };
  const result = run.data;
  const preview = run.variables?.dry_run === true;

  return (
    <form
      aria-label={text.bulk.legend}
      className="mb-4 flex flex-col gap-2 rounded-md border border-line bg-surface p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <FormRow>
        <Select
          className="w-56"
          label={text.bulk.scope}
          value={effective}
          options={[
            { value: 'selected', label: text.bulk.scopeSelected(selected.length) },
            ...(selectedOnly ? [] : [{ value: 'filter', label: text.bulk.scopeFilter }]),
          ]}
          onChange={(event) => {
            setTarget(event.target.value === 'filter' ? 'filter' : 'selected');
          }}
        />
        <Select
          className="w-40"
          label={text.bulk.action}
          value={action}
          options={[
            { value: 'policy', label: text.bulk.actions.policy },
            { value: 'dismiss', label: text.bulk.actions.dismiss },
          ]}
          onChange={(event) => {
            setAction(event.target.value === 'dismiss' ? 'dismiss' : 'policy');
          }}
        />
        {action === 'policy' && (
          <Select
            className="w-64"
            label={text.decide.policy}
            value={policy}
            options={POLICIES.map((value) => ({ value, label: text.decide.policies[value] }))}
            onChange={(event) => {
              setPolicy(POLICIES.find((p) => p === event.target.value) ?? 'authority');
            }}
          />
        )}
        <Input
          className="min-w-48 flex-1"
          label={text.bulk.reason}
          value={reason}
          aria-required
          error={attempted && reason.trim() === '' ? text.decide.reasonRequired : undefined}
          onChange={(event) => {
            setReason(event.target.value);
          }}
        />
        <FormAction>
          <Button
            disabled={run.isPending}
            onClick={() => {
              submit(true);
            }}
          >
            {text.bulk.preview}
          </Button>
          <Button type="submit" variant="primary" disabled={run.isPending}>
            {text.bulk.apply}
          </Button>
        </FormAction>
      </FormRow>
      {attempted && empty && <p className="text-xs text-danger">{text.bulk.nothingSelected}</p>}
      {run.error !== null && (
        <p role="alert" className="text-sm text-danger">
          {decisionError(run.error)}
        </p>
      )}
      {result !== undefined && run.error === null && (
        <p role="status" className="text-sm">
          {preview
            ? text.bulk.previewResult(result.count)
            : text.bulk.result(
                result.count,
                Object.entries(result.counts)
                  .map(([status, count]) => text.bulk.countPart(status, count))
                  .join(', '),
              )}
        </p>
      )}
    </form>
  );
}
