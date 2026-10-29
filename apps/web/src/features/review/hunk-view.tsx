import { useId, type ReactNode } from 'react';
import { validateValue, type ValueSpec } from '@/api/values';
import { review as reviewMessages } from '@/messages/review';
import { Button } from '@/ui/button';
import { Textarea } from '@/ui/field';
import { cn } from '@/ui/cn';
import { DiffText, type SideBranches } from './side-by-side';
import {
  openHunks,
  passageTexts,
  pickText,
  type Hunk,
  type HunkPick,
  type PickKind,
} from './text-hunks';

const text = reviewMessages.hunks;
const sideText = reviewMessages.sides;

/** Unchanged passages longer than this fold away so the conflicting ones stay in view. */
const FOLD_AFTER = 200;

export const PICK_KINDS: PickKind[] = [
  'ours',
  'theirs',
  'ours_theirs',
  'theirs_ours',
  'base',
  'edit',
];

/** Number keys of the passage view, in the order of `PICK_KINDS` plus `0` for the base. */
export const PICK_KEYS: Record<string, Exclude<PickKind, 'edit'>> = {
  '1': 'ours',
  '2': 'theirs',
  '3': 'ours_theirs',
  '4': 'theirs_ours',
  '0': 'base',
};

export interface TextHunksProps {
  hunks: Hunk[];
  /** Chosen way of each conflicting passage, by index into `hunks`. */
  picks: Record<number, HunkPick>;
  /** The whole text once every conflicting passage has a pick. */
  composed: string | null;
  /** Index of the passage the number keys act on. */
  active: number;
  /** Id used to name the radio groups of this Conflict. */
  groupId: string;
  branches?: SideBranches;
  /** The custom text stored for the Conflict, if any. */
  storedText: string | null;
  /** What the Attribute allows, for the length and the warning. */
  spec: ValueSpec | null;
  readOnly: boolean;
  saving: boolean;
  onPick: (index: number, pick: HunkPick) => void;
  onActive: (index: number) => void;
  onUse: (value: string) => void;
}

function Passage({
  hunk,
  index,
  number,
  total,
  pick,
  active,
  groupId,
  branches,
  readOnly,
  onPick,
  onActive,
}: {
  hunk: Hunk;
  index: number;
  number: number;
  total: number;
  pick: HunkPick | undefined;
  active: boolean;
  groupId: string;
  branches?: SideBranches;
  readOnly: boolean;
  onPick: (index: number, pick: HunkPick) => void;
  onActive: (index: number) => void;
}) {
  const side = (name: 'ours' | 'theirs') =>
    branches === undefined ? sideText[name] : sideText.withBranch(sideText[name], branches[name]);
  const columns: { key: string; title: string; body: ReactNode }[] = [
    { key: 'base', title: sideText.base, body: hunk.base },
    { key: 'ours', title: side('ours'), body: <DiffText base={hunk.base} value={hunk.ours} /> },
    {
      key: 'theirs',
      title: side('theirs'),
      body: <DiffText base={hunk.base} value={hunk.theirs} />,
    },
  ];
  const choose = (kind: PickKind) => {
    if (kind !== 'edit') {
      onPick(index, { kind });
      return;
    }
    onPick(index, { kind, text: pick === undefined ? hunk.ours : pickText(hunk, pick) });
  };
  return (
    <div
      role="group"
      aria-label={text.passage(number, total)}
      data-hunk={index}
      tabIndex={-1}
      onFocus={() => {
        onActive(index);
      }}
      className={cn(
        'flex flex-col gap-2 rounded-md border bg-surface p-3',
        active ? 'border-accent' : 'border-line',
      )}
    >
      <div className="grid gap-2 sm:grid-cols-3">
        {columns.map((column) => (
          <div key={column.key} data-side={column.key} className="min-w-0">
            <p className="text-xs font-medium text-muted">{column.title}</p>
            <p className="font-mono text-xs break-words whitespace-pre-wrap">{column.body}</p>
          </div>
        ))}
      </div>
      <div role="radiogroup" aria-label={text.choiceFor(number)} className="flex flex-wrap gap-3">
        {PICK_KINDS.map((kind) => (
          <label key={kind} className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name={`${groupId}-${index.toString()}`}
              checked={pick?.kind === kind}
              disabled={readOnly}
              onChange={() => {
                choose(kind);
              }}
            />
            {text.choices[kind]}
          </label>
        ))}
      </div>
      {pick?.kind === 'edit' && (
        <Textarea
          label={text.editLabel(number)}
          hideLabel
          disabled={readOnly}
          value={pick.text}
          onChange={(event) => {
            onPick(index, { kind: 'edit', text: event.target.value });
          }}
        />
      )}
    </div>
  );
}

function Preview({
  hunks,
  picks,
  composed: built,
  storedText,
  spec,
}: Pick<TextHunksProps, 'hunks' | 'picks' | 'composed' | 'storedText' | 'spec'>) {
  const total = hunks.filter((h) => h.status === 'conflict').length;
  const open = openHunks(hunks, picks);
  const showStored = storedText !== null && Object.keys(picks).length === 0;
  const composed = showStored ? storedText : built;
  const problem = composed === null || spec === null ? null : validateValue(spec, composed);
  let number = 0;
  return (
    <div className="flex flex-col gap-1">
      <h5 className="text-xs font-medium text-muted">{text.preview}</h5>
      <p role="status" className="text-xs text-muted">
        {showStored ? text.savedText : open > 0 ? text.open(open, total) : text.allChosen}
      </p>
      <p className="rounded-md border border-line p-2 font-mono text-xs break-words whitespace-pre-wrap">
        {showStored
          ? storedText
          : passageTexts(hunks, picks).map((part, index) => {
              if (hunks[index]?.status === 'conflict') number += 1;
              return part === null ? (
                <mark key={index} className="bg-warning/20">
                  {text.previewOpen(number)}
                </mark>
              ) : (
                <span key={index}>{part}</span>
              );
            })}
      </p>
      {composed !== null && (
        <p className="text-xs text-muted">
          {text.length(composed.length, spec?.constraints?.max_length)}
          {problem !== null && <span className="ml-2 text-warning">{text.violates(problem)}</span>}
        </p>
      )}
    </div>
  );
}

/**
 * The text of a DV1 Conflict as passages: unchanged ones as muted text, each conflicting one
 * as a card with the three versions and a choice, and a live preview of the composed text.
 */
export function TextHunks(props: TextHunksProps) {
  const { hunks, composed, active, groupId, branches, readOnly, saving, onPick, onActive } = props;
  const { picks } = props;
  const headingId = useId();
  const total = hunks.filter((h) => h.status === 'conflict').length;
  const items: ReactNode[] = [];
  let number = 0;
  for (let index = 0; index < hunks.length; index += 1) {
    const hunk = hunks[index];
    if (hunk === undefined) continue;
    if (hunk.status === 'conflict') {
      number += 1;
      items.push(
        <Passage
          key={index}
          hunk={hunk}
          index={index}
          number={number}
          total={total}
          pick={picks[index]}
          active={active === index}
          groupId={groupId}
          branches={branches}
          readOnly={readOnly}
          onPick={onPick}
          onActive={onActive}
        />,
      );
      continue;
    }
    // Consecutive unchanged passages read as one.
    let run = hunk.merged ?? '';
    while (hunks[index + 1]?.status === 'clean') {
      index += 1;
      run += hunks[index]?.merged ?? '';
    }
    items.push(
      run.length > FOLD_AFTER ? (
        <details key={index} className="text-xs text-muted">
          <summary className="cursor-pointer">{text.unchanged(run.length)}</summary>
          <p className="mt-1 break-words whitespace-pre-wrap">{run}</p>
        </details>
      ) : (
        <p key={index} className="text-xs break-words whitespace-pre-wrap text-muted">
          {run}
        </p>
      ),
    );
  }
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-3">
      <h4 id={headingId} className="text-xs font-medium text-muted">
        {text.legend}
      </h4>
      {items}
      <Preview {...props} />
      <p className="text-xs text-muted">{text.keys}</p>
      <div>
        <Button
          variant="primary"
          disabled={readOnly || saving || composed === null}
          onClick={() => {
            if (composed !== null) props.onUse(composed);
          }}
        >
          {text.useMerged}
        </Button>
      </div>
    </section>
  );
}
