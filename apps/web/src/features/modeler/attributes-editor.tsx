import { useState } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import type { AttributeConstraints } from '@/api/types';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { modeler as modelerMessages } from '@/messages/modeler';
import type { AttributeDocument } from './document';
import {
  enumValuesOf,
  errorAt,
  listOfText,
  newAttribute,
  removeKey,
  typeFromLabel,
  typeLabel,
  typeOptions,
  type ElementIds,
} from './form-helpers';

export interface AttributesEditorProps {
  attributes: Record<string, AttributeDocument>;
  /** Document path of the `attributes` mapping, for example `kinds/Asset/attributes`. */
  path: string;
  issues: SchemaIssue[];
  kindNames: string[];
  ids: ElementIds;
  onChange: (next: Record<string, AttributeDocument>) => void;
  onRename: (from: string, to: string) => void;
}

const NUMBER_CONSTRAINTS = ['min', 'max', 'min_length', 'max_length'] as const;
const t = modelerMessages.attributes;

function Check({
  label,
  name,
  checked,
  onChange,
}: {
  label: string;
  name: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => {
          onChange(event.target.checked);
        }}
      />
      <span className="sr-only">{t.flagOf(label, name)}</span>
      <span aria-hidden>{label}</span>
    </label>
  );
}

function Constraints({
  name,
  value,
  error,
  onChange,
}: {
  name: string;
  value: AttributeConstraints;
  error: string | undefined;
  onChange: (next: AttributeConstraints) => void;
}) {
  const set = (key: keyof AttributeConstraints, raw: string) => {
    const next: Record<string, unknown> = { ...value };
    if (raw === '') delete next[key];
    else next[key] = key === 'pattern' ? raw : Number(raw);
    onChange(next);
  };
  return (
    <details open={error !== undefined}>
      <summary className="cursor-pointer text-xs text-muted">{t.constraintsOf(name)}</summary>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
        {NUMBER_CONSTRAINTS.map((key) => (
          <Input
            key={key}
            label={t.constraintOf(t.constraints[key], name)}
            type="number"
            value={value[key] ?? ''}
            onChange={(event) => {
              set(key, event.target.value);
            }}
          />
        ))}
        <Input
          label={t.constraintOf(t.constraints.pattern, name)}
          value={value.pattern ?? ''}
          onChange={(event) => {
            set('pattern', event.target.value);
          }}
        />
      </div>
      {error !== undefined && <p className="mt-1 text-xs text-danger">{error}</p>}
    </details>
  );
}

interface RowProps {
  name: string;
  attribute: AttributeDocument;
  path: string;
  issues: SchemaIssue[];
  kindNames: string[];
  taken: (name: string) => boolean;
  onChange: (next: AttributeDocument) => void;
  onRename: (to: string) => void;
  onRemove: () => void;
}

function AttributeRow({
  name,
  attribute,
  path,
  issues,
  kindNames,
  taken,
  onChange,
  onRename,
  onRemove,
}: RowProps) {
  const [typed, setTyped] = useState<string | null>(null);
  const label = typeLabel(attribute.type);
  const values = enumValuesOf(attribute.type);
  const isEnum = label === 'enum' || label === 'array<enum>';
  const update = (patch: Partial<AttributeDocument>) => {
    const next: Record<string, unknown> = { ...attribute, ...patch };
    for (const key of Object.keys(next))
      if (next[key] === undefined || next[key] === false) delete next[key];
    onChange(next as unknown as AttributeDocument);
  };
  const shown = typed ?? name;
  const nameProblem =
    typed !== null && typed !== name && taken(typed)
      ? modelerMessages.elements.attributeExists(typed)
      : undefined;
  const rowPath = `${path}/${name}`;

  return (
    <li className="flex flex-col gap-2 rounded-md border border-line bg-surface p-3">
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-start">
        <Input
          label={t.nameOf(name)}
          value={shown}
          error={nameProblem ?? errorAt(issues, rowPath)}
          onChange={(event) => {
            const next = event.target.value;
            setTyped(next);
            if (next !== '' && !taken(next)) onRename(next);
          }}
          onBlur={() => {
            setTyped(null);
          }}
        />
        <Select
          label={t.typeOf(name)}
          value={label}
          error={errorAt(issues, `${rowPath}/type`)}
          options={typeOptions(kindNames, label).map((value) => ({ value, label: value }))}
          onChange={(event) => {
            update({ type: typeFromLabel(event.target.value, values) });
          }}
        />
        {isEnum ? (
          <Input
            label={t.enumValuesOf(name)}
            hint={modelerMessages.elements.commaSeparated}
            value={values.join(', ')}
            onChange={(event) => {
              update({ type: typeFromLabel(label, listOfText(event.target.value)) });
            }}
          />
        ) : (
          <Input
            label={t.descriptionOf(name)}
            value={attribute.description ?? ''}
            onChange={(event) => {
              update({ description: event.target.value === '' ? undefined : event.target.value });
            }}
          />
        )}
        <Button
          variant="ghost"
          aria-label={t.removeNamed(name)}
          onClick={onRemove}
          className="sm:mt-5"
        >
          {t.remove}
        </Button>
      </div>
      <div className="flex flex-wrap gap-4">
        <Check
          label={t.required}
          name={name}
          checked={attribute.required === true}
          onChange={(checked) => {
            update({ required: checked });
          }}
        />
        <Check
          label={t.unique}
          name={name}
          checked={attribute.unique === true}
          onChange={(checked) => {
            update({ unique: checked });
          }}
        />
        <Check
          label={t.indexed}
          name={name}
          checked={attribute.indexed === true}
          onChange={(checked) => {
            update({ indexed: checked });
          }}
        />
      </div>
      <Constraints
        name={name}
        value={attribute.constraints ?? {}}
        error={errorAt(issues, `${rowPath}/constraints`) ?? errorAt(issues, `${rowPath}/default`)}
        onChange={(next) => {
          update({ constraints: Object.keys(next).length === 0 ? undefined : next });
        }}
      />
    </li>
  );
}

/** Attributes of a Kind or a Relation: name, type, constraints and flags, with inline issues. */
export function AttributesEditor({
  attributes,
  path,
  issues,
  kindNames,
  ids,
  onChange,
  onRename,
}: AttributesEditorProps) {
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-2" aria-label={t.listLabel(path)}>
        {Object.entries(attributes).map(([name, attribute]) => (
          <AttributeRow
            key={attribute.id ?? name}
            name={name}
            attribute={attribute}
            path={path}
            issues={issues}
            kindNames={kindNames}
            taken={(candidate) => candidate in attributes}
            onChange={(next) => {
              onChange({ ...attributes, [name]: next });
            }}
            onRename={(to) => {
              onRename(name, to);
            }}
            onRemove={() => {
              onChange(removeKey(attributes, name));
            }}
          />
        ))}
      </ul>
      <div>
        <Button
          size="sm"
          onClick={() => {
            const [name, attribute] = newAttribute(attributes, ids, path);
            onChange({ ...attributes, [name]: attribute });
          }}
        >
          {t.add}
        </Button>
      </div>
    </div>
  );
}
