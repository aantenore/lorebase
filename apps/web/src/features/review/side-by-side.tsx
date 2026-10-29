import { useMemo, useState } from 'react';
import type { Conflict, JsonValue } from '@/api/types';
import { review as reviewMessages } from '@/messages/review';
import { cn } from '@/ui/cn';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { namePath, type Names } from './conflict-meta';
import { wordDiff, type DiffPart } from './text-diff';

const text = reviewMessages.sides;

type Side = 'base' | 'ours' | 'theirs';
const SIDES: Side[] = ['base', 'ours', 'theirs'];

type Flat = Map<string, JsonValue>;

/** Leaf values by dotted path. Lists and empty objects stay whole; a bare value has the path ''. */
export function flatten(value: JsonValue, prefix = '', into: Flat = new Map()): Flat {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    const entries = Object.entries(value);
    if (entries.length > 0) {
      for (const [key, inner] of entries) {
        flatten(inner, prefix === '' ? key : `${prefix}.${key}`, into);
      }
      return into;
    }
  }
  into.set(prefix, value);
  return into;
}

const same = (a: JsonValue | undefined, b: JsonValue | undefined) =>
  JSON.stringify(a) === JSON.stringify(b);

function show(value: JsonValue | undefined): string {
  if (value === undefined) return text.absent;
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export interface DiffRow {
  path: string;
  base: JsonValue | undefined;
  ours: JsonValue | undefined;
  theirs: JsonValue | undefined;
  /** Which sides moved away from the base. */
  changed: Record<'ours' | 'theirs', boolean>;
}

/** Rows of the three-way comparison: one per path that appears on any side. */
export function diffRows(conflict: Pick<Conflict, Side>): DiffRow[] {
  const flats = {
    base: flatten(conflict.base),
    ours: flatten(conflict.ours),
    theirs: flatten(conflict.theirs),
  };
  const paths = new Set<string>();
  for (const side of ['ours', 'theirs', 'base'] as const) {
    if (conflict[side] !== null) for (const path of flats[side].keys()) paths.add(path);
  }
  return [...paths].map((path) => {
    const row = {
      path,
      base: conflict.base === null ? undefined : flats.base.get(path),
      ours: conflict.ours === null ? undefined : flats.ours.get(path),
      theirs: conflict.theirs === null ? undefined : flats.theirs.get(path),
    };
    return {
      ...row,
      changed: {
        ours: !same(row.ours, row.base),
        theirs: !same(row.theirs, row.base),
      },
    };
  });
}

const SIDE_TITLE: Record<Side, string> = { base: text.base, ours: text.ours, theirs: text.theirs };

/** The Branches the sides come from: ours is the target, theirs the source. */
export interface SideBranches {
  ours: string;
  theirs: string;
}

function titleOf(side: Side, branches: SideBranches | undefined): string {
  return side === 'base' || branches === undefined
    ? SIDE_TITLE[side]
    : text.withBranch(SIDE_TITLE[side], branches[side]);
}

/** A string side read against the base word by word. */
function WordDiff({ parts }: { parts: DiffPart[] }) {
  return (
    <>
      {parts.map((part, index) => {
        const after = parts[index - 1]?.op === 'delete';
        if (part.op === 'equal') return <span key={index}>{part.text}</span>;
        const inserted = part.op === 'insert';
        const Mark = inserted ? 'ins' : 'del';
        return (
          <Mark
            key={index}
            className={cn(
              'rounded-sm',
              // Keeps a removed run and the run that replaces it apart without adding text.
              inserted && after && 'ml-1',
              inserted ? 'bg-success/20 no-underline' : 'bg-danger/20 line-through select-none',
            )}
          >
            <span className="sr-only">{inserted ? text.added : text.removed}</span>
            {part.text}
          </Mark>
        );
      })}
    </>
  );
}

function Cell({ row, side }: { row: DiffRow; side: Side }) {
  const value = row[side];
  const changed = side !== 'base' && row.changed[side];
  const parts = useMemo(
    () =>
      changed && typeof row.base === 'string' && typeof value === 'string'
        ? wordDiff(row.base, value)
        : null,
    [changed, row.base, value],
  );
  return (
    <TD
      data-side={side}
      data-changed={changed}
      className={cn(
        'font-mono text-xs break-words whitespace-pre-wrap',
        changed && 'bg-warning/10',
        value === undefined && 'text-muted italic',
      )}
    >
      {changed && <span className="sr-only">{text.changed}</span>}
      {parts === null ? show(value) : <WordDiff parts={parts} />}
    </TD>
  );
}

function MemberTable({
  conflict,
  branches,
}: {
  conflict: Conflict;
  branches: SideBranches | undefined;
}) {
  const raw = conflict.details['members'];
  const members =
    typeof raw === 'object' && raw !== null && !Array.isArray(raw)
      ? (raw as Record<string, JsonValue>)
      : {};
  return (
    <Table caption={text.membersCaption(conflict.kind)}>
      <THead>
        <TR>
          <TH>{text.member}</TH>
          {SIDES.map((side) => (
            <TH key={side}>{titleOf(side, branches)}</TH>
          ))}
        </TR>
      </THead>
      <TBody>
        {conflict.sample.map((member) => {
          const detail = members[member];
          const sides =
            typeof detail === 'object' && detail !== null && !Array.isArray(detail) ? detail : {};
          return (
            <TR key={member}>
              <TH scope="row" className="font-mono text-xs">
                {member}
              </TH>
              {SIDES.map((side) => (
                <TD key={side} data-side={side} className="font-mono text-xs">
                  {show(side in sides ? sides[side] : undefined)}
                </TD>
              ))}
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}

/**
 * Base, ours and theirs side by side. Records and schema documents are compared path by
 * path (attribute by attribute, property by property); only the paths where a side moved
 * from the base are listed unless the reader asks for the rest.
 */
export function SideBySide({
  conflict,
  names,
  branches,
}: {
  conflict: Conflict;
  names: Names;
  branches?: SideBranches;
}) {
  const [all, setAll] = useState(false);
  const noSides = SIDES.every((side) => conflict[side] === null);
  if (noSides) {
    return conflict.sample.length > 0 ? (
      <MemberTable conflict={conflict} branches={branches} />
    ) : null;
  }
  const rows = diffRows(conflict);
  const visible = all ? rows : rows.filter((r) => r.changed.ours || r.changed.theirs);
  return (
    <div className="flex flex-col gap-2">
      <Table caption={text.caption}>
        <THead>
          <TR>
            <TH>{text.path}</TH>
            {SIDES.map((side) => (
              <TH key={side}>{titleOf(side, branches)}</TH>
            ))}
          </TR>
        </THead>
        <TBody>
          {visible.map((row) => (
            <TR key={row.path}>
              <TH scope="row" className="font-mono text-xs font-medium">
                {row.path === '' ? text.value : namePath(row.path, names)}
              </TH>
              {SIDES.map((side) => (
                <Cell key={side} row={row} side={side} />
              ))}
            </TR>
          ))}
        </TBody>
      </Table>
      {rows.length > visible.length || all ? (
        <label className="flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={all}
            onChange={(event) => {
              setAll(event.target.checked);
            }}
          />
          {text.showUnchanged}
        </label>
      ) : null}
    </div>
  );
}
