import { useId, type KeyboardEvent } from 'react';
import type { Conflict, ConflictCode, Resolution } from '@/api/types';
import { Badge } from '@/ui/layout';
import { CODE_LABEL, compareCodes, isGrouped, locationOf, type Names } from './conflict-meta';

export interface ConflictGroup {
  code: ConflictCode;
  conflicts: Conflict[];
}

/** Conflicts grouped by kind, kinds in code order, Conflicts in the order given. */
export function groupByKind(conflicts: Conflict[]): ConflictGroup[] {
  const groups = new Map<ConflictCode, Conflict[]>();
  for (const conflict of conflicts) {
    groups.set(conflict.kind, [...(groups.get(conflict.kind) ?? []), conflict]);
  }
  return [...groups]
    .sort(([a], [b]) => compareCodes(a, b))
    .map(([code, members]) => ({ code, conflicts: members }));
}

/** The order the list shows Conflicts in, which is also the order the keyboard walks. */
export function listOrder(conflicts: Conflict[]): Conflict[] {
  return groupByKind(conflicts).flatMap((group) => group.conflicts);
}

/** The Conflict `step` places after `from` in `order`, wrapping at both ends. */
export function neighbor(
  order: Conflict[],
  from: string | null,
  step: 1 | -1,
): Conflict | undefined {
  if (order.length === 0) return undefined;
  const index = order.findIndex((c) => c.fingerprint === from);
  if (index < 0) return order[step === 1 ? 0 : order.length - 1];
  return order[(index + step + order.length) % order.length];
}

export interface ConflictListProps {
  conflicts: Conflict[];
  resolutions: Resolution[];
  selected: string | null;
  names: Names;
  onSelect: (fingerprint: string) => void;
}

/**
 * Conflicts grouped by kind, as one listbox. Arrow keys, `j` and `k` move the selection,
 * Home and End jump to the ends; the selected Conflict opens in the resolver beside it.
 */
export function ConflictList({
  conflicts,
  resolutions,
  selected,
  names,
  onSelect,
}: ConflictListProps) {
  const base = useId();
  const order = listOrder(conflicts);
  const idOf = (conflict: Conflict) => `${base}-${conflict.fingerprint}`;
  const active = order.find((c) => c.fingerprint === selected);

  const onKeyDown = (event: KeyboardEvent) => {
    const key = event.key;
    let target: Conflict | undefined;
    if (key === 'ArrowDown' || key === 'j') target = neighbor(order, selected, 1);
    else if (key === 'ArrowUp' || key === 'k') target = neighbor(order, selected, -1);
    else if (key === 'Home') target = order[0];
    else if (key === 'End') target = order[order.length - 1];
    else return;
    event.preventDefault();
    if (target !== undefined) onSelect(target.fingerprint);
  };

  return (
    <div
      role="listbox"
      aria-label="Conflicts"
      aria-activedescendant={active === undefined ? undefined : idOf(active)}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex flex-col gap-3 rounded-md border border-line bg-surface p-2 focus-visible:outline-2 focus-visible:outline-ring"
    >
      {groupByKind(conflicts).map((group) => (
        <div key={group.code} role="group" aria-label={`${group.code} ${CODE_LABEL[group.code]}`}>
          <h3 className="mb-1 px-2 text-xs font-semibold text-muted uppercase">
            {group.code} {CODE_LABEL[group.code]} ({group.conflicts.length})
          </h3>
          <ul className="flex flex-col gap-1">
            {group.conflicts.map((conflict) => {
              const stored = resolutions.find((r) => r.fingerprint === conflict.fingerprint);
              const isSelected = conflict.fingerprint === selected;
              return (
                <li
                  key={conflict.fingerprint}
                  id={idOf(conflict)}
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => {
                    onSelect(conflict.fingerprint);
                  }}
                  className={
                    isSelected
                      ? 'cursor-pointer rounded-md bg-surface-2 px-2 py-1.5 outline outline-accent'
                      : 'cursor-pointer rounded-md px-2 py-1.5 hover:bg-surface-2/60'
                  }
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs break-all">
                      {locationOf(conflict, names)}
                    </span>
                    {stored === undefined ? (
                      <Badge tone="warning">open</Badge>
                    ) : (
                      <Badge tone="success">{stored.choice}</Badge>
                    )}
                  </div>
                  {isGrouped(conflict) && (
                    <span className="text-xs text-muted">{conflict.count} members</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}
