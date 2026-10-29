import { useState } from 'react';
import { useRecord } from '@/api/queries';
import type { ReconciliationCandidate } from '@/api/reconciliation-types';
import type { JsonValue, Kind } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Button } from '@/ui/button';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { ReasonField, type Decide } from './decide';
import { mergeAttributes, showValue } from './reconcile-state';

const text = reconcileMessages;

/**
 * The decision on a duplicate pair: the Record that stays and, per Attribute that differs, the
 * value to keep. Keeper writes it on a review Branch and opens a Proposal; Dismiss takes the
 * same reason.
 */
export function MergeForm({
  decide,
  candidate,
  kind,
}: {
  decide: Decide;
  candidate: ReconciliationCandidate;
  kind: Kind;
}) {
  const scope = useScope();
  const [firstKey = '', secondKey = ''] = candidate.keys;
  const first = useRecord(scope, kind.api_name, firstKey);
  const second = useRecord(scope, kind.api_name, secondKey);
  const [survivor, setSurvivor] = useState(firstKey);
  // The key whose value each differing Attribute keeps; the survivor's when not picked.
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [reason, setReason] = useState('');
  const [attempted, setAttempted] = useState(false);

  if (first.data === undefined || second.data === undefined) {
    const failed = first.error ?? second.error;
    return failed === null ? (
      <p role="status" className="text-sm text-muted">
        {text.merge.loading}
      </p>
    ) : (
      <p role="alert" className="text-sm text-danger">
        {failed.message}
      </p>
    );
  }
  const values: Record<string, Record<string, JsonValue>> = {
    [firstKey]: first.data.record.attributes as Record<string, JsonValue>,
    [secondKey]: second.data.record.attributes as Record<string, JsonValue>,
  };
  // Sensitive Attributes are never offered: their values may be masked here.
  const { pickable: differing, kept } = mergeAttributes(
    kind,
    values[firstKey] ?? {},
    values[secondKey] ?? {},
  );
  const absorbed = survivor === firstKey ? secondKey : firstKey;
  const chosen = (attribute: string) => picks[attribute] ?? survivor;

  const submit = (dryRun: boolean) => {
    setAttempted(true);
    if (reason.trim() === '') return;
    const taken = Object.fromEntries(
      differing
        .filter((attribute) => chosen(attribute) !== survivor)
        .map((attribute) => [attribute, values[absorbed]?.[attribute] ?? null]),
    );
    decide.mutate({
      kind: 'resolve',
      input: { choice: 'merge', survivor, values: taken, reason: reason.trim(), dry_run: dryRun },
    });
  };

  return (
    <form
      aria-label={text.merge.title}
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1 text-xs font-medium text-muted">{text.merge.survivor}</legend>
        {candidate.keys.map((key) => (
          <label key={key} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`survivor-${candidate.id}`}
              checked={survivor === key}
              onChange={() => {
                setSurvivor(key);
                setPicks({});
              }}
            />
            {text.merge.survivorOption(key)}
          </label>
        ))}
        <p className="text-xs text-muted">{text.merge.absorbed(absorbed)}</p>
      </fieldset>

      {kept.length > 0 && <p className="text-xs text-muted">{text.merge.sensitiveKept(kept)}</p>}

      {differing.length === 0 ? (
        kept.length === 0 && <p className="text-sm text-muted">{text.merge.same}</p>
      ) : (
        <Table caption={text.merge.valuesCaption}>
          <THead>
            <TR>
              <TH>{text.merge.attribute}</TH>
              {candidate.keys.map((key) => (
                <TH key={key}>
                  <RecordLink domain={scope.domain} recordRef={`${kind.api_name}/${key}`}>
                    {text.merge.keep(key)}
                  </RecordLink>
                </TH>
              ))}
            </TR>
          </THead>
          <TBody>
            {differing.map((attribute) => (
              <TR key={attribute}>
                <TH scope="row" className="font-normal">
                  {attribute}
                </TH>
                {candidate.keys.map((key) => {
                  const value = values[key]?.[attribute];
                  return (
                    <TD key={key}>
                      <label className="flex items-start gap-2">
                        <input
                          type="radio"
                          className="mt-1"
                          name={`pick-${candidate.id}-${attribute}`}
                          aria-label={text.merge.pick(attribute, key)}
                          checked={chosen(attribute) === key}
                          onChange={() => {
                            setPicks((current) => ({ ...current, [attribute]: key }));
                          }}
                        />
                        <span className="font-mono text-xs break-words whitespace-pre-wrap">
                          {value === undefined ? text.merge.absent : showValue(value)}
                        </span>
                      </label>
                    </TD>
                  );
                })}
              </TR>
            ))}
          </TBody>
        </Table>
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
          {text.merge.apply}
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
