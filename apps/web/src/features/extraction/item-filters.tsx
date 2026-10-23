import { useState } from 'react';
import type { ExtractionItem, ItemDecision, ItemType } from '@/api/extraction-types';
import { extraction as extractionMessages } from '@/messages/extraction';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { FormAction, FormRow } from '@/ui/layout';
import {
  boundOf,
  isFiltering,
  NO_ITEM_FILTER,
  percent,
  TYPE_LABEL,
  type ItemFilterState,
} from './item-state';

const copy = extractionMessages;

const STATUS_OPTIONS = (['all', 'pending', 'accepted', 'rejected'] as const).map((value) => ({
  value,
  label: copy.filters.statusOptions[value],
}));

export function FilterBar({
  targets,
  filter,
  onChange,
}: {
  targets: string[];
  filter: ItemFilterState;
  onChange: (filter: ItemFilterState) => void;
}) {
  const bad = (value: string) =>
    value.trim() !== '' && boundOf(value) === undefined ? copy.filters.badNumber : undefined;
  return (
    <form
      aria-label={copy.filters.label}
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <FormRow>
        <Select
          label={copy.filters.status}
          value={filter.status}
          options={STATUS_OPTIONS}
          onChange={(event) => {
            onChange({ ...filter, status: event.target.value as ItemDecision | 'all' });
          }}
        />
        <Select
          label={copy.filters.target}
          value={filter.target}
          options={[
            { value: 'all', label: copy.filters.all },
            ...targets.map((name) => ({ value: name, label: name })),
          ]}
          onChange={(event) => {
            onChange({ ...filter, target: event.target.value });
          }}
        />
        <Select
          label={copy.filters.type}
          value={filter.type}
          options={[
            { value: 'all', label: copy.filters.all },
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
          label={copy.filters.min}
          className="w-36"
          inputMode="decimal"
          placeholder={copy.filters.minPlaceholder}
          value={filter.min}
          error={bad(filter.min)}
          onChange={(event) => {
            onChange({ ...filter, min: event.target.value });
          }}
        />
        <Input
          label={copy.filters.max}
          className="w-36"
          inputMode="decimal"
          placeholder={copy.filters.maxPlaceholder}
          value={filter.max}
          error={bad(filter.max)}
          onChange={(event) => {
            onChange({ ...filter, max: event.target.value });
          }}
        />
        <FormAction>
          <Button
            disabled={!isFiltering(filter)}
            onClick={() => {
              onChange(NO_ITEM_FILTER);
            }}
          >
            {copy.filters.clear}
          </Button>
        </FormAction>
      </FormRow>
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
  const shownPercent = threshold === undefined ? '' : percent(threshold);
  return (
    <FormRow role="group" aria-label={copy.bulk.label}>
      <FormAction>
        <Button
          disabled={disabled || n === 0}
          onClick={() => {
            onStage(shown, 'accepted');
          }}
        >
          {copy.bulk.accept(n)}
        </Button>
        <Button
          disabled={disabled || n === 0}
          onClick={() => {
            onStage(shown, 'rejected');
          }}
        >
          {copy.bulk.reject(n)}
        </Button>
        <Button
          variant="ghost"
          disabled={disabled || n === 0}
          onClick={() => {
            onStage(shown, 'pending');
          }}
        >
          {copy.bulk.reset(n)}
        </Button>
      </FormAction>
      <Input
        label={copy.bulk.threshold}
        className="w-28"
        inputMode="decimal"
        value={text}
        error={threshold === undefined ? copy.filters.badNumber : undefined}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
      <FormAction>
        <Button
          disabled={disabled || above.length === 0}
          onClick={() => {
            onStage(above, 'accepted');
          }}
        >
          {copy.bulk.acceptAbove(above.length, shownPercent)}
        </Button>
        <Button
          disabled={disabled || below.length === 0}
          onClick={() => {
            onStage(below, 'rejected');
          }}
        >
          {copy.bulk.rejectBelow(below.length, shownPercent)}
        </Button>
      </FormAction>
    </FormRow>
  );
}
