import { useState } from 'react';
import type { JsonValue } from '@/api/types';
import { parseAttributeType, type ValueSpec } from '@/api/values';
import { Input, Select, Textarea } from './field';

export interface ValueInputProps {
  label: string;
  /** What the value must be. `null` accepts any JSON, edited as text. */
  spec: ValueSpec | null;
  value: JsonValue | undefined;
  /** `parseError` is set while the text typed so far cannot become a value. */
  onChange: (value: JsonValue | undefined, parseError: string | null) => void;
  /** Why the value is not acceptable, shown under the field. */
  error?: string | null;
  hideLabel?: boolean;
  disabled?: boolean;
}

const EMPTY = '';

function toText(value: JsonValue | undefined, json: boolean): string {
  if (value === undefined || value === null) return EMPTY;
  if (json) return JSON.stringify(value, null, 2);
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function Problem({ error }: { error: string | null | undefined }) {
  if (error === null || error === undefined) return null;
  return <p className="text-xs text-danger">{error}</p>;
}

/** Whether the type is edited as JSON text: lists, `json`, points and untyped values. */
function isJsonType(spec: ValueSpec | null): boolean {
  if (spec === null) return true;
  const parsed = parseAttributeType(spec.type);
  return parsed === null || parsed.array || parsed.base === 'json' || parsed.base === 'geo_point';
}

function JsonField({ label, value, onChange, error, hideLabel, disabled }: ValueInputProps) {
  const [text, setText] = useState(() => toText(value, true));
  const [parseError, setParseError] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-1">
      <Textarea
        label={label}
        hideLabel={hideLabel === true}
        disabled={disabled === true}
        value={text}
        aria-invalid={error !== null && error !== undefined}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          if (next.trim() === EMPTY) {
            setParseError(null);
            onChange(null, null);
            return;
          }
          try {
            const parsed = JSON.parse(next) as JsonValue;
            setParseError(null);
            onChange(parsed, null);
          } catch {
            setParseError('Not valid JSON.');
            onChange(undefined, 'Not valid JSON.');
          }
        }}
      />
      <Problem error={parseError ?? error} />
    </div>
  );
}

function ScalarField(props: ValueInputProps) {
  const { label, spec, value, onChange, error, hideLabel, disabled } = props;
  const base = spec === null ? null : parseAttributeType(spec.type)?.base;
  const common = {
    label,
    hideLabel: hideLabel === true,
    disabled: disabled === true,
    'aria-invalid': error !== null && error !== undefined,
  };

  let field;
  if (base === 'boolean') {
    field = (
      <Select
        {...common}
        value={value === true ? 'true' : value === false ? 'false' : EMPTY}
        options={[
          { value: EMPTY, label: 'No value' },
          { value: 'true', label: 'Yes' },
          { value: 'false', label: 'No' },
        ]}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === EMPTY ? null : next === 'true', null);
        }}
      />
    );
  } else if (base === 'enum') {
    const options = spec?.constraints?.enum_values ?? [];
    field = (
      <Select
        {...common}
        value={typeof value === 'string' ? value : EMPTY}
        options={[
          { value: EMPTY, label: 'No value' },
          ...options.map((option) => ({ value: option, label: option })),
        ]}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === EMPTY ? null : next, null);
        }}
      />
    );
  } else if (base === 'integer' || base === 'decimal' || base === 'float') {
    field = (
      <Input
        {...common}
        type="number"
        step={base === 'integer' ? 1 : 'any'}
        value={typeof value === 'number' ? String(value) : EMPTY}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === EMPTY ? null : Number(next), null);
        }}
      />
    );
  } else if (base === 'text') {
    field = (
      <Textarea
        {...common}
        value={typeof value === 'string' ? value : EMPTY}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === EMPTY ? null : next, null);
        }}
      />
    );
  } else {
    field = (
      <Input
        {...common}
        type={base === 'date' ? 'date' : 'text'}
        value={typeof value === 'string' ? value : toText(value, false)}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === EMPTY ? null : next, null);
        }}
      />
    );
  }
  return (
    <div className="flex flex-col gap-1">
      {field}
      <Problem error={error} />
    </div>
  );
}

/**
 * Type-aware input for one Attribute value: a number field for numbers, a select for
 * enums and booleans, a date field for dates, a text area for long text, JSON text for
 * lists, points and values of unknown type. An empty field means no value (`null`).
 */
export function ValueInput(props: ValueInputProps) {
  return isJsonType(props.spec) ? <JsonField {...props} /> : <ScalarField {...props} />;
}
