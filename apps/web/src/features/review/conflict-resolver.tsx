import { useEffect, useRef, useState } from 'react';
import type { Conflict, JsonValue, Resolution, ResolutionChoice } from '@/api/types';
import { validateValue, type ValueSpec } from '@/api/values';
import { Button } from '@/ui/button';
import { review as reviewMessages } from '@/messages/review';
import { Badge, FormAction, FormRow } from '@/ui/layout';
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
import { SideBySide, type SideBranches } from './side-by-side';

const text = reviewMessages.resolver;

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
  /** The Branches behind ours and theirs, named in the column headers when known. */
  branches?: SideBranches;
  /** Opens the next open Conflict; the `n` key calls it. */
  onNext?: () => void;
  /** Tells whether the draft differs from what is stored, so the caller can warn before leaving. */
  onDirtyChange?: (dirty: boolean) => void;
}

const COPY_SIDES = ['base', 'ours', 'theirs'] as const;

const SHORTCUT_CHOICES: Record<string, ResolutionChoice> = {
  o: 'ours',
  t: 'theirs',
  b: 'base',
  e: 'custom',
};

const NON_TEXT_INPUTS = new Set(['radio', 'checkbox', 'button', 'submit', 'reset']);

/** Whether a key press lands in a field that takes typed text, CodeMirror included. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.closest('.cm-editor') !== null) return true;
  if (target instanceof HTMLInputElement) return !NON_TEXT_INPUTS.has(target.type);
  return target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}

/** JSON text with object keys in sorted order, so equal values compare equal. */
function canonical(value: JsonValue | undefined): string | undefined {
  if (typeof value !== 'object' || value === null) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((v) => canonical(v) ?? 'null').join(',')}]`;
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(value[key]) ?? 'null'}`)
    .join(',')}}`;
}

function sameResolution(a: Resolution, b: Resolution): boolean {
  return (
    a.choice === b.choice &&
    canonical(a.value ?? null) === canonical(b.value ?? null) &&
    canonical(a.values ?? null) === canonical(b.values ?? null)
  );
}

/** Targets that act on their own on Enter, or that belong to the page around the resolver. */
const OWN_ENTER = 'a, button, summary, [role="button"], [role="listbox"], [role="option"]';

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
  if (typed === undefined) return text.enterValue;
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
  // The input keeps its own text, so a copied value is shown by mounting a fresh input.
  const [version, setVersion] = useState(0);
  return (
    <div className="flex flex-col gap-2">
      <ValueInput
        key={version}
        label={conflict.kind === 'SC3' ? text.newName : text.customValue}
        spec={spec}
        value={typed === undefined ? seedValue(conflict) : typed.value}
        error={problem}
        disabled={disabled}
        onChange={(value, parseError) => {
          onChange({ value, parseError });
        }}
      />
      <div role="group" aria-label={text.copyLegend} className="flex flex-wrap gap-2">
        {COPY_SIDES.map((side) => (
          <Button
            key={side}
            size="sm"
            disabled={disabled || conflict[side] === null}
            onClick={() => {
              onChange({ value: conflict[side], parseError: null });
              setVersion((v) => v + 1);
            }}
          >
            {text.copySide[side]}
          </Button>
        ))}
      </div>
    </div>
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
      <FormRow>
        <ValueInput
          label={text.sameForAll}
          spec={spec}
          value={all.value}
          error={allProblem}
          disabled={disabled}
          onChange={(value, parseError) => {
            setAll({ value, parseError });
          }}
        />
        <FormAction>
          <Button
            disabled={disabled || all.value === undefined || allProblem !== null}
            onClick={() => {
              for (const member of conflict.sample) onChange(member, all);
              setVersion((v) => v + 1);
            }}
          >
            {text.applyToAll}
          </Button>
        </FormAction>
      </FormRow>
      <Table caption={text.membersCaption}>
        <THead>
          <TR>
            <TH>{text.member}</TH>
            <TH>{text.newValue}</TH>
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
                  label={text.valueFor(member)}
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

/** The stored resolution in a form that is equal whenever the resolutions are. */
function storedKey(resolution: Resolution | undefined): string {
  return resolution === undefined
    ? ''
    : `${resolution.choice}|${canonical(resolution.value ?? null) ?? ''}|${canonical(resolution.values ?? null) ?? ''}`;
}

function initialSingle(resolution: Resolution | undefined): Typed | undefined {
  return resolution?.choice !== 'custom' || resolution.value === undefined
    ? undefined
    : { value: resolution.value, parseError: null };
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
        return {
          resolution: null,
          problem: text.memberProblem(member, problem ?? text.enterValue),
        };
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
  branches,
  onNext,
  onDirtyChange,
}: ConflictResolverProps) {
  const [choice, setChoice] = useState<ResolutionChoice | null>(resolution?.choice ?? null);
  const [single, setSingle] = useState<Typed | undefined>(() => initialSingle(resolution));
  const [members, setMembers] = useState<Record<string, Typed>>(() => initialMembers(resolution));
  // Set by the person's own edits; the draft follows the stored resolution until then.
  const [touched, setTouched] = useState(false);
  const [seen, setSeen] = useState(() => storedKey(resolution));
  const stored = storedKey(resolution);
  if (seen !== stored) {
    setSeen(stored);
    if (!touched) {
      setChoice(resolution?.choice ?? null);
      setSingle(initialSingle(resolution));
      setMembers(initialMembers(resolution));
    }
  }
  const grouped = isGrouped(conflict);
  const rejected = conflict.details['rejected'];

  const draft =
    choice === null
      ? { resolution: null, problem: text.pickChoice }
      : buildResolution(conflict, choice, single, members, spec);
  const canSave = !readOnly && !saving && draft.resolution !== null;
  const dirty =
    !readOnly &&
    touched &&
    (resolution === undefined
      ? choice !== null
      : draft.resolution === null || !sameResolution(draft.resolution, resolution));

  const save = () => {
    if (!readOnly && !saving && draft.resolution !== null) onSave(draft.resolution);
  };

  const sectionRef = useRef<HTMLElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const focusEditor = useRef(false);
  const focusInEditor = () => {
    if (editorRef.current === null) return;
    focusEditor.current = false;
    editorRef.current.querySelector<HTMLElement>('input, textarea, select')?.focus();
  };
  // Runs after every render: the editor exists only once the choice is custom.
  useEffect(() => {
    if (focusEditor.current) focusInEditor();
  });

  // The latest handlers are read through refs so the listeners attach once.
  const reportDirty = useRef(onDirtyChange);
  const onKey = useRef<(event: KeyboardEvent) => void>(() => undefined);
  useEffect(() => {
    reportDirty.current = onDirtyChange;
    onKey.current = (event) => {
      if (readOnly || event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      if (isTyping(target)) return;
      if (target instanceof Element && target.closest('[role="dialog"]') !== null) return;
      const takesEnter = (el: EventTarget | null) =>
        el === document.body ||
        (el instanceof Element &&
          sectionRef.current?.contains(el) === true &&
          el.closest(OWN_ENTER) === null);
      const key = event.key.toLowerCase();
      const picked = SHORTCUT_CHOICES[key];
      if (picked !== undefined) {
        if (!conflict.choices.includes(picked)) return;
        setTouched(true);
        setChoice(picked);
        if (picked === 'custom') {
          focusEditor.current = true;
          if (choice === 'custom') focusInEditor();
        }
      } else if (key === 's' || (key === 'enter' && takesEnter(target))) {
        if (!canSave) return;
        save();
      } else if (key === 'n' && onNext !== undefined) {
        onNext();
      } else {
        return;
      }
      event.preventDefault();
    };
  });
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      onKey.current(event);
    };
    document.addEventListener('keydown', listener);
    return () => {
      document.removeEventListener('keydown', listener);
    };
  }, []);
  useEffect(() => {
    reportDirty.current?.(dirty);
  }, [dirty]);
  useEffect(() => {
    const report = reportDirty;
    return () => {
      report.current?.(false);
    };
  }, []);

  return (
    <section
      ref={sectionRef}
      aria-label={text.section(conflict.kind, locationOf(conflict, names))}
      className="flex min-w-0 flex-col gap-4 rounded-md border border-line bg-surface p-4"
    >
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="danger">{conflict.kind}</Badge>
          <h3 className="text-sm font-semibold">{CODE_LABEL[conflict.kind]}</h3>
          {grouped && <Badge>{reviewMessages.conflicts.members(conflict.count)}</Badge>}
          {conflict.machine_resolvable && <Badge tone="accent">{text.policyCanResolve}</Badge>}
          {resolution !== undefined && (
            <Badge tone="success">{text.resolved(resolution.choice)}</Badge>
          )}
        </div>
        <p className="font-mono text-xs text-muted">{locationOf(conflict, names)}</p>
        <p className="text-sm">{messageOf(conflict, names)}</p>
      </header>

      {typeof rejected === 'string' && (
        <p role="alert" className="rounded-md border border-danger p-2 text-sm text-danger">
          {text.refused(rejected)}
        </p>
      )}

      <SideBySide conflict={conflict} names={names} branches={branches} />

      <fieldset className="flex flex-col gap-2" disabled={readOnly}>
        <legend className="mb-1 text-xs font-medium text-muted">{text.legend}</legend>
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
                  setTouched(true);
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

      {choice === 'custom' && (
        <div ref={editorRef}>
          {grouped ? (
            <CustomMembers
              conflict={conflict}
              spec={spec}
              values={members}
              disabled={readOnly}
              onChange={(member, typed) => {
                setTouched(true);
                setMembers((current) => ({ ...current, [member]: typed }));
              }}
            />
          ) : (
            <CustomSingle
              conflict={conflict}
              spec={spec}
              typed={single}
              disabled={readOnly}
              onChange={(typed) => {
                setTouched(true);
                setSingle(typed);
              }}
            />
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" disabled={!canSave} onClick={save}>
          {resolution === undefined ? text.save : text.update}
        </Button>
        {draft.resolution === null && choice === 'custom' && grouped && (
          <span className="text-xs text-muted">{draft.problem}</span>
        )}
      </div>
    </section>
  );
}
