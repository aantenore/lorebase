import { useState } from 'react';
import type { Conflict, ConflictCode, ResolutionChoice } from '@/api/types';
import { review as reviewMessages } from '@/messages/review';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { FormAction, FormRow } from '@/ui/layout';
import { bulkChoiceLabel, CODE_LABEL } from './conflict-meta';
import type { ConflictFilter, StatusFilter } from './conflict-state';
import { groupByKind } from './conflict-list';

const filters = reviewMessages.filters;

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: filters.statusOptions.all },
  { value: 'open', label: filters.statusOptions.open },
  { value: 'resolved', label: filters.statusOptions.resolved },
];

export function FilterBar({
  conflicts,
  filter,
  onChange,
}: {
  /** Every Conflict of the attempt, so the type list does not shrink while filtering. */
  conflicts: Conflict[];
  filter: ConflictFilter;
  onChange: (filter: ConflictFilter) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <Select
        label={filters.type}
        value={filter.kind}
        options={[
          { value: 'all', label: filters.allTypes },
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
        label={filters.status}
        value={filter.status}
        options={STATUS_OPTIONS}
        onChange={(event) => {
          onChange({ ...filter, status: event.target.value as StatusFilter });
        }}
      />
      <Input
        label={filters.find}
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
    <FormRow>
      <Select
        label={filters.bulkChoice}
        value={choice}
        options={choices.map((value) => ({ value, label: bulkChoiceLabel(open, value) }))}
        onChange={(event) => {
          setPicked(event.target.value as ResolutionChoice);
        }}
      />
      <FormAction>
        <Button
          disabled={disabled || targets.length === 0}
          onClick={() => {
            onApply(choice, targets);
          }}
        >
          {filters.bulkApply(bulkChoiceLabel(targets, choice), targets.length)}
        </Button>
      </FormAction>
    </FormRow>
  );
}
