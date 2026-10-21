import { useState } from 'react';
import type { Conflict, JsonValue, Resolution, ResolutionChoice } from '@/api/types';
import { validateValue, type ValueSpec } from '@/api/values';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { ValueInput } from '@/ui/value-input';
import {
  CODE_LABEL,
  choiceHint,
  choiceLabel,
  isGrouped,
  locationOf,
  messageOf,
  type Names,
} from './conflict-meta';
import { SideBySide } from './side-by-side';

export interface ConflictResolverProps {
  conflict: Conflict;
  /** The stored Resolution of this Conflict, if any. */
  resolution: Resolution | undefined;
  names: Names;
  /** What a custom value must be; `null` takes any JSON. */
  spec: ValueSpec | null;
  /** True once the Proposal is merged or closed. */
  readOnly?: boolean;
  saving?: boolean;
  onSave: (resolution: Resolution) => void;
}

interface Typed {
  value: JsonValue | undefined;
  parseError: string | null;
}

/** The first value shown in the editor of a Conflict that has none stored yet. */
function seedValue(conflict: Conflict): JsonValue | undefined {
  const side = [conflict.ours, conflict.theirs, conflict.base].find((v) => v !== null);
  return side;
}

function problemOf(spec: ValueSpec | null, typed: Typed | undefined): string | null {
  if (typed === undefined) return 'Enter a value.';
  if (typed.parseError !== null) return typed.parseError;
  if (spec === null) return null;
  return validateValue(spec, typed.value ?? null);
}

function CustomSingle({
  conflict,
  spec,
  typed,
  onChange,
  disabled,
}: {
  conflict: Conflict;
  spec: ValueSpec | null;
  typed: Typed | undefined;
  onChange: (typed: Typed) => void;
  disabled: boolean;
}) {
  const problem = typed === undefined ? null : problemOf(spec, typed);
  return (
    <ValueInput
      label={conflict.kind === 'SC3' ? 'New name' : 'Custom value'}
      spec={spec}
      value={typed === undefined ? seedValue(conflict) : typed.value}
      error={problem}
      disabled={disabled}
      onChange={(value, parseError) => {
        onChange({ value, parseError });
      }}
    />
  );
}

function CustomMembers({
  conflict,
  spec,
  values,
  onChange,
  disabled,
}: {
  conflict: Conflict;
  spec: ValueSpec | null;
  values: Record<string, Typed>;
  onChange: (member: string, typed: Typed) => void;
  disabled: boolean;
}) {
  const [all, setAll] = useState<Typed>({ value: undefined, parseError: null });
  const [version, setVersion] = useState(0);
  const allProblem = all.value === undefined ? null : problemOf(spec, all);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-2">
        <ValueInput
          label="Same value for every member"
          spec={spec}
          value={all.value}
          error={allProblem}
          disabled={disabled}
          onChange={(value, parseError) => {
            setAll({ value, parseError });
          }}
        />
        <Button
          size="sm"
          disabled={disabled || all.value === undefined || allProblem !== null}
          onClick={() => {
            for (const member of conflict.sample) onChange(member, all);
            setVersion((v) => v + 1);
          }}
        >
          Apply to all members
        </Button>
      </div>
      <Table caption="Values per member">
        <THead>
          <TR>
            <TH>Member</TH>
            <TH>New value</TH>
          </TR>
        </THead>
        <TBody>
          {conflict.sample.map((member) => (
            <TR key={member}>
              <TH scope="row" className="font-mono text-xs">
                {member}
              </TH>
              <TD>
                <ValueInput
                  key={`${member}-${version}`}
                  label={`Value for ${member}`}
                  hideLabel
                  spec={spec}
                  value={values[member]?.value}
                  error={values[member] === undefined ? null : problemOf(spec, values[member])}
                  disabled={disabled}
                  onChange={(value, parseError) => {
                    onChange(member, { value, parseError });
                  }}
                />
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function initialMembers(resolution: Resolution | undefined): Record<string, Typed> {
  return Object.fromEntries(
    Object.entries(resolution?.values ?? {}).map(([member, value]) => [
      member,
      { value, parseError: null },
    ]),
  );
}

function buildResolution(
  conflict: Conflict,
  choice: ResolutionChoice,
  single: Typed | undefined,
  members: Record<string, Typed>,
  spec: ValueSpec | null,
): { resolution: Resolution | null; problem: string | null } {
  if (choice !== 'custom') {
    return { resolution: { fingerprint: conflict.fingerprint, choice }, problem: null };
  }
  if (isGrouped(conflict)) {
    const values: Record<string, JsonValue> = {};
    for (const member of conflict.sample) {
      const typed = members[member];
      const problem = problemOf(spec, typed);
      if (typed === undefined || problem !== null) {
        return { resolution: null, problem: `${member}: ${problem ?? 'Enter a value.'}` };
      }
      values[member] = typed.value ?? null;
    }
    return { resolution: { fingerprint: conflict.fingerprint, choice, values }, problem: null };
  }
  const typed = single ?? { value: seedValue(conflict), parseError: null };
  const problem = problemOf(spec, typed);
  if (problem !== null) return { resolution: null, problem };
  return {
    resolution: { fingerprint: conflict.fingerprint, choice, value: typed.value ?? null },
    problem: null,
  };
}

/**
 * Resolver for one Conflict: base, ours and theirs side by side, the choices its kind
 * allows and, for `custom`, a type-aware editor (one value, or one per member of a group).
 * Render it with `key={conflict.fingerprint}` so its draft resets when the Conflict changes.
 */
export function ConflictResolver({
  conflict,
  resolution,
  names,
  spec,
  readOnly = false,
  saving = false,
  onSave,
}: ConflictResolverProps) {
  const [choice, setChoice] = useState<ResolutionChoice | null>(resolution?.choice ?? null);
  const [single, setSingle] = useState<Typed | undefined>(
    resolution?.value === undefined ? undefined : { value: resolution.value, parseError: null },
  );
  const [members, setMembers] = useState<Record<string, Typed>>(() => initialMembers(resolution));
  const grouped = isGrouped(conflict);
  const rejected = conflict.details['rejected'];

  const draft =
    choice === null
      ? { resolution: null, problem: 'Pick a choice.' }
      : buildResolution(conflict, choice, single, members, spec);

  return (
    <section
      aria-label={`Conflict ${conflict.kind} ${locationOf(conflict, names)}`}
      className="flex flex-col gap-4 rounded-md border border-line bg-surface p-4"
    >
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="danger">{conflict.kind}</Badge>
          <h3 className="text-sm font-semibold">{CODE_LABEL[conflict.kind]}</h3>
          {grouped && <Badge>{conflict.count} members</Badge>}
          {conflict.machine_resolvable && <Badge tone="accent">policy can resolve</Badge>}
          {resolution !== undefined && <Badge tone="success">resolved: {resolution.choice}</Badge>}
        </div>
        <p className="font-mono text-xs text-muted">{locationOf(conflict, names)}</p>
        <p className="text-sm">{messageOf(conflict, names)}</p>
      </header>

      {typeof rejected === 'string' && (
        <p role="alert" className="rounded-md border border-danger p-2 text-sm text-danger">
          The last merge run refused the stored resolution: {rejected}
        </p>
      )}

      <SideBySide conflict={conflict} names={names} />

      <fieldset className="flex flex-col gap-2" disabled={readOnly}>
        <legend className="mb-1 text-xs font-medium text-muted">Resolution</legend>
        {conflict.choices.map((option) => {
          const hint = choiceHint(conflict, option);
          return (
            <label key={option} className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name={`choice-${conflict.fingerprint}`}
                className="mt-1"
                checked={choice === option}
                onChange={() => {
                  setChoice(option);
                }}
              />
              <span>
                {choiceLabel(conflict, option)}
                {hint !== undefined && <span className="block text-xs text-muted">{hint}</span>}
              </span>
            </label>
          );
        })}
      </fieldset>

      {choice === 'custom' &&
        (grouped ? (
          <CustomMembers
            conflict={conflict}
            spec={spec}
            values={members}
            disabled={readOnly}
            onChange={(member, typed) => {
              setMembers((current) => ({ ...current, [member]: typed }));
            }}
          />
        ) : (
          <CustomSingle
            conflict={conflict}
            spec={spec}
            typed={single}
            disabled={readOnly}
            onChange={setSingle}
          />
        ))}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="primary"
          disabled={readOnly || saving || draft.resolution === null}
          onClick={() => {
            if (draft.resolution !== null) onSave(draft.resolution);
          }}
        >
          {resolution === undefined ? 'Save resolution' : 'Update resolution'}
        </Button>
        {draft.resolution === null && choice === 'custom' && grouped && (
          <span className="text-xs text-muted">{draft.problem}</span>
        )}
      </div>
    </section>
  );
}
