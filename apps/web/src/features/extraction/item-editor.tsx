import { useState } from 'react';
import type { ExtractionItem, ItemEdits } from '@/api/extraction-types';
import type { Attribute, JsonValue, Schema } from '@/api/types';
import { validateValue, parseAttributeType, type ValueSpec } from '@/api/values';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { ValueInput } from '@/ui/value-input';

type Typed = { value: JsonValue | undefined; parseError: string | null };

/** What a value must be: the Attribute's type and constraints, or any JSON when unknown. */
function specOf(attribute: Attribute | undefined): ValueSpec | null {
  if (attribute === undefined || parseAttributeType(attribute.type) === null) return null;
  return {
    type: attribute.type,
    required: attribute.required,
    constraints: attribute.constraints,
  };
}

function problemOf(spec: ValueSpec | null, typed: Typed | undefined): string | null {
  if (typed === undefined) return null;
  if (typed.parseError !== null) return typed.parseError;
  return spec === null ? null : validateValue(spec, typed.value ?? null);
}

function attributesFor(item: ExtractionItem, schema: Schema | undefined): Attribute[] {
  if (schema === undefined) return [];
  if (item.type === 'link') {
    const relation = schema.relations.find((r) => r.api_name === item.relation);
    return Object.values(relation?.attributes ?? {});
  }
  const kind = schema.kinds.find((k) => k.api_name === item.kind);
  return Object.values(kind?.attributes ?? {}).filter((a) => a.derivation_id === null);
}

/** The whole value of the field now: what the person typed, else what the item holds. */
const sameJson = (a: unknown, b: unknown) =>
  JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/**
 * Inline edits of an item: the values of a new Record, the value of an Attribute, or the
 * endpoints and values of a Link. "Stage edits" keeps them with the decision until the
 * decisions are applied; values are checked against the Attribute's type and constraints.
 * Render it with a `key` that changes with the item so the draft resets.
 */
export function ItemEditor({
  item,
  schema,
  disabled,
  onStage,
}: {
  item: ExtractionItem;
  schema: Schema | undefined;
  disabled: boolean;
  onStage: (edits: ItemEdits) => void;
}) {
  const attributes = attributesFor(item, schema);
  const [typed, setTyped] = useState<Record<string, Typed>>({});
  const [one, setOne] = useState<Typed | undefined>(undefined);
  const [from, setFrom] = useState(item.from ?? '');
  const [to, setTo] = useState(item.to ?? '');

  const attributeOf = (name: string | null) => attributes.find((a) => a.api_name === name);
  const fieldNames = item.type === 'value' ? [] : attributes.map((a) => a.api_name);
  // A key or an Attribute the schema does not know is still shown, so nothing is hidden.
  const names = [...new Set([...fieldNames, ...Object.keys(item.values)])];

  const valueSpec = specOf(attributeOf(item.attribute));
  const problems: string[] = [];
  if (item.type === 'value' && one !== undefined) {
    const problem = problemOf(valueSpec, one);
    if (problem !== null) problems.push(problem);
  }
  for (const name of names) {
    const problem = problemOf(specOf(attributeOf(name)), typed[name]);
    if (problem !== null) problems.push(`${name}: ${problem}`);
  }
  if (item.type === 'link' && (from.trim() === '' || to.trim() === '')) {
    problems.push('Both ends of a Link need a key.');
  }

  const stage = () => {
    const edits: ItemEdits = {};
    if (item.type === 'value' && one !== undefined && !sameJson(one.value, item.value)) {
      edits.value = one.value ?? null;
    }
    if (item.type !== 'value') {
      const values: Record<string, JsonValue> = {};
      for (const [name, entry] of Object.entries(typed)) {
        if (!sameJson(entry.value, item.values[name])) values[name] = entry.value ?? null;
      }
      if (Object.keys(values).length > 0) edits.values = values;
    }
    if (item.type === 'link') {
      if (from.trim() !== item.from) edits.from = from.trim();
      if (to.trim() !== item.to) edits.to = to.trim();
    }
    onStage(edits);
  };

  return (
    <form
      aria-label="Edit the proposal"
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (problems.length === 0) stage();
      }}
    >
      {item.type === 'link' && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            label={`From (${item.relation ?? 'Record'} key)`}
            value={from}
            disabled={disabled}
            onChange={(event) => {
              setFrom(event.target.value);
            }}
          />
          <Input
            label="To (Record key)"
            value={to}
            disabled={disabled}
            onChange={(event) => {
              setTo(event.target.value);
            }}
          />
        </div>
      )}
      {item.type === 'value' && (
        <ValueInput
          label={`Value of ${item.attribute ?? 'the Attribute'}`}
          spec={valueSpec}
          value={one === undefined ? item.value : one.value}
          error={one === undefined ? null : problemOf(valueSpec, one)}
          disabled={disabled}
          onChange={(value, parseError) => {
            setOne({ value, parseError });
          }}
        />
      )}
      {item.type !== 'value' &&
        names.map((name) => {
          const spec = specOf(attributeOf(name));
          const entry = typed[name];
          return (
            <ValueInput
              key={name}
              label={name}
              spec={spec}
              value={entry === undefined ? item.values[name] : entry.value}
              error={problemOf(spec, entry)}
              disabled={disabled}
              onChange={(value, parseError) => {
                setTyped((current) => ({ ...current, [name]: { value, parseError } }));
              }}
            />
          );
        })}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={disabled || problems.length > 0}>
          Stage edits
        </Button>
        {problems.length > 0 && <span className="text-xs text-danger">{problems[0]}</span>}
      </div>
    </form>
  );
}
