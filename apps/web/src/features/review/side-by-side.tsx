import { useState } from 'react';
import type { Conflict, JsonValue } from '@/api/types';
import { cn } from '@/ui/cn';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { namePath, type Names } from './conflict-meta';

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
  if (value === undefined) return 'absent';
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

const SIDE_TITLE: Record<Side, string> = { base: 'Base', ours: 'Ours', theirs: 'Theirs' };

function Cell({ row, side }: { row: DiffRow; side: Side }) {
  const value = row[side];
  const changed = side !== 'base' && row.changed[side];
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
      {changed && <span className="sr-only">changed: </span>}
      {show(value)}
    </TD>
  );
}

function MemberTable({ conflict }: { conflict: Conflict }) {
  const raw = conflict.details['members'];
  const members =
    typeof raw === 'object' && raw !== null && !Array.isArray(raw)
      ? (raw as Record<string, JsonValue>)
      : {};
  return (
    <Table caption={`Members of ${conflict.kind}`}>
      <THead>
        <TR>
          <TH>Member</TH>
          {SIDES.map((side) => (
            <TH key={side}>{SIDE_TITLE[side]}</TH>
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
export function SideBySide({ conflict, names }: { conflict: Conflict; names: Names }) {
  const [all, setAll] = useState(false);
  const noSides = SIDES.every((side) => conflict[side] === null);
  if (noSides) {
    return conflict.sample.length > 0 ? <MemberTable conflict={conflict} /> : null;
  }
  const rows = diffRows(conflict);
  const visible = all ? rows : rows.filter((r) => r.changed.ours || r.changed.theirs);
  return (
    <div className="flex flex-col gap-2">
      <Table caption="Base, ours and theirs">
        <THead>
          <TR>
            <TH>Path</TH>
            {SIDES.map((side) => (
              <TH key={side}>{SIDE_TITLE[side]}</TH>
            ))}
          </TR>
        </THead>
        <TBody>
          {visible.map((row) => (
            <TR key={row.path}>
              <TH scope="row" className="font-mono text-xs font-medium">
                {row.path === '' ? 'value' : namePath(row.path, names)}
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
          Show unchanged paths
        </label>
      ) : null}
    </div>
  );
}
