import { useState } from 'react';
import type { Conflict, ConflictCode, ResolutionChoice } from '@/api/types';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { CODE_LABEL } from './conflict-meta';
import type { ConflictFilter, StatusFilter } from './conflict-state';
import { groupByKind } from './conflict-list';

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'resolved', label: 'Resolved' },
];

export function FilterBar({
  conflicts,
  filter,
  onChange,
}: {
  /** Every Conflict of the attempt, so the kind list does not shrink while filtering. */
  conflicts: Conflict[];
  filter: ConflictFilter;
  onChange: (filter: ConflictFilter) => void;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <Select
        label="Kind"
        value={filter.kind}
        options={[
          { value: 'all', label: 'All kinds' },
          ...groupByKind(conflicts).map((group) => ({
            value: group.code,
            label: `${group.code} ${CODE_LABEL[group.code]} (${group.conflicts.length})`,
          })),
        ]}
        onChange={(event) => {
          onChange({ ...filter, kind: event.target.value as ConflictCode | 'all' });
        }}
      />
      <Select
        label="Status"
        value={filter.status}
        options={STATUS_OPTIONS}
        onChange={(event) => {
          onChange({ ...filter, status: event.target.value as StatusFilter });
        }}
      />
      <Input
        label="Find a Conflict"
        type="search"
        value={filter.text}
        onChange={(event) => {
          onChange({ ...filter, text: event.target.value });
        }}
      />
    </div>
  );
}

/** Choices that need no value and that at least one of the Conflicts accepts. */
export function bulkChoices(conflicts: Conflict[]): ResolutionChoice[] {
  const found = new Set<ResolutionChoice>();
  for (const conflict of conflicts) {
    for (const choice of conflict.choices) if (choice !== 'custom') found.add(choice);
  }
  return [...found];
}

/**
 * One choice for every open Conflict in the current filter that accepts it. Conflicts
 * that do not accept it are left alone, and the button says how many it will touch.
 */
export function BulkBar({
  open,
  disabled,
  onApply,
}: {
  /** Open Conflicts of the current filter. */
  open: Conflict[];
  disabled: boolean;
  onApply: (choice: ResolutionChoice, targets: Conflict[]) => void;
}) {
  const choices = bulkChoices(open);
  const [picked, setPicked] = useState<ResolutionChoice | null>(null);
  const choice = picked !== null && choices.includes(picked) ? picked : (choices[0] ?? null);
  if (choice === null) return null;
  const targets = open.filter((c) => c.choices.includes(choice));
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Select
        label="Bulk choice"
        value={choice}
        options={choices.map((value) => ({ value, label: value }))}
        onChange={(event) => {
          setPicked(event.target.value as ResolutionChoice);
        }}
      />
      <Button
        disabled={disabled || targets.length === 0}
        onClick={() => {
          onApply(choice, targets);
        }}
      >
        Apply {choice} to {targets.length} open
      </Button>
    </div>
  );
}
