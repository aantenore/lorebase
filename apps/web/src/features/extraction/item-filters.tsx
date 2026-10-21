import { useState } from 'react';
import type { ExtractionItem, ItemDecision, ItemType } from '@/api/extraction-types';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import {
  boundOf,
  isFiltering,
  NO_ITEM_FILTER,
  percent,
  TYPE_LABEL,
  type ItemFilterState,
} from './item-state';

export function FilterBar({
  targets,
  filter,
  onChange,
}: {
  targets: string[];
  filter: ItemFilterState;
  onChange: (filter: ItemFilterState) => void;
}) {
  const bad = (text: string) =>
    text.trim() !== '' && boundOf(text) === undefined ? 'A number from 0 to 1.' : undefined;
  return (
    <form
      aria-label="Filter items"
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <Select
        label="Status"
        value={filter.status}
        options={[
          { value: 'all', label: 'All' },
          { value: 'pending', label: 'Pending' },
          { value: 'accepted', label: 'Accepted' },
          { value: 'rejected', label: 'Rejected' },
        ]}
        onChange={(event) => {
          onChange({ ...filter, status: event.target.value as ItemDecision | 'all' });
        }}
      />
      <Select
        label="Kind or Relation"
        value={filter.target}
        options={[
          { value: 'all', label: 'All' },
          ...targets.map((name) => ({ value: name, label: name })),
        ]}
        onChange={(event) => {
          onChange({ ...filter, target: event.target.value });
        }}
      />
      <Select
        label="Type"
        value={filter.type}
        options={[
          { value: 'all', label: 'All' },
          ...(Object.keys(TYPE_LABEL) as ItemType[]).map((type) => ({
            value: type,
            label: TYPE_LABEL[type],
          })),
        ]}
        onChange={(event) => {
          onChange({ ...filter, type: event.target.value as ItemType | 'all' });
        }}
      />
      <Input
        label="Confidence at least"
        className="w-36"
        inputMode="decimal"
        placeholder="0.0"
        value={filter.min}
        error={bad(filter.min)}
        onChange={(event) => {
          onChange({ ...filter, min: event.target.value });
        }}
      />
      <Input
        label="Confidence below"
        className="w-36"
        inputMode="decimal"
        placeholder="1.0"
        value={filter.max}
        error={bad(filter.max)}
        onChange={(event) => {
          onChange({ ...filter, max: event.target.value });
        }}
      />
      <Button
        disabled={!isFiltering(filter)}
        onClick={() => {
          onChange(NO_ITEM_FILTER);
        }}
      >
        Clear filters
      </Button>
    </form>
  );
}

/**
 * Bulk decisions over the items the filter shows, or over the pending ones by confidence.
 * They stage decisions like a click on every item would; nothing is sent until "Apply".
 */
export function BulkBar({
  shown,
  defaultThreshold,
  disabled,
  onStage,
}: {
  shown: readonly ExtractionItem[];
  defaultThreshold: number;
  disabled: boolean;
  onStage: (items: readonly ExtractionItem[], decision: ItemDecision) => void;
}) {
  const [text, setText] = useState(defaultThreshold.toString());
  const threshold = boundOf(text);
  const pending = shown.filter((item) => item.decision === 'pending');
  const above =
    threshold === undefined ? [] : pending.filter((i) => (i.confidence ?? 0) >= threshold);
  const below =
    threshold === undefined ? [] : pending.filter((i) => (i.confidence ?? 0) < threshold);
  const n = shown.length;
  return (
    <div role="group" aria-label="Bulk decisions" className="flex flex-wrap items-end gap-2">
      <Button
        disabled={disabled || n === 0}
        onClick={() => {
          onStage(shown, 'accepted');
        }}
      >
        Accept {n} shown
      </Button>
      <Button
        disabled={disabled || n === 0}
        onClick={() => {
          onStage(shown, 'rejected');
        }}
      >
        Reject {n} shown
      </Button>
      <Button
        variant="ghost"
        disabled={disabled || n === 0}
        onClick={() => {
          onStage(shown, 'pending');
        }}
      >
        Reset {n} shown
      </Button>
      <span className="mx-1 h-6 border-l border-line" aria-hidden="true" />
      <Input
        label="Threshold"
        className="w-28"
        inputMode="decimal"
        value={text}
        error={threshold === undefined ? 'A number from 0 to 1.' : undefined}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
      <Button
        disabled={disabled || above.length === 0}
        onClick={() => {
          onStage(above, 'accepted');
        }}
      >
        Accept {above.length} pending at or above{' '}
        {threshold === undefined ? '' : percent(threshold)}
      </Button>
      <Button
        disabled={disabled || below.length === 0}
        onClick={() => {
          onStage(below, 'rejected');
        }}
      >
        Reject {below.length} pending below {threshold === undefined ? '' : percent(threshold)}
      </Button>
    </div>
  );
}
