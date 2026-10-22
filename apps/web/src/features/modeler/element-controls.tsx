import { useState } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { errorAt } from './form-helpers';
import { NewName } from './kind-form';

const STATUSES = ['draft', 'active', 'deprecated'];

/** Choose an element of a section, add one by name, remove the chosen one. */
export function ElementPicker({
  what,
  names,
  selected,
  onSelect,
  onAdd,
  onRemove,
}: {
  /** `Operation`, `Derivation`, `Source`. */
  what: string;
  names: string[];
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
  onAdd: (name: string) => void;
  onRemove: (name: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      {selected !== undefined && (
        <Select
          label={what}
          value={selected}
          options={names.map((n) => ({ value: n, label: n }))}
          onChange={(event) => {
            onSelect(event.target.value);
          }}
        />
      )}
      <NewName
        label={`New ${what} name`}
        button={`Add ${what}`}
        taken={(candidate) => names.includes(candidate)}
        onAdd={(added) => {
          onAdd(added);
          onSelect(added);
        }}
      />
      {selected !== undefined && (
        <Button
          variant="danger"
          onClick={() => {
            onRemove(selected);
            onSelect(undefined);
          }}
        >
          Remove {what}
        </Button>
      )}
    </div>
  );
}

/** The api name of an element; a rename applies as soon as the new name is free. */
export function ElementName({
  what,
  name,
  names,
  issues,
  path,
  onRename,
  onRenamed,
}: {
  what: string;
  name: string;
  names: string[];
  issues: SchemaIssue[];
  path: string;
  onRename: (from: string, to: string) => void;
  onRenamed: (name: string) => void;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const taken = typed !== null && typed !== name && names.includes(typed);
  return (
    <Input
      label={`${what} name`}
      value={typed ?? name}
      error={taken ? `${typed} exists already.` : errorAt(issues, path)}
      onChange={(event) => {
        const next = event.target.value;
        setTyped(next);
        if (next !== '' && !names.includes(next)) {
          onRename(name, next);
          onRenamed(next);
        }
      }}
      onBlur={() => {
        setTyped(null);
      }}
    />
  );
}

/** Status and description of an element; `active` and an empty description are left out. */
export function StatusAndDescription({
  what,
  element,
  onChange,
}: {
  what: string;
  element: Record<string, unknown>;
  onChange: (key: 'status' | 'description', value: string | undefined) => void;
}) {
  const status = typeof element['status'] === 'string' ? element['status'] : 'active';
  const description = typeof element['description'] === 'string' ? element['description'] : '';
  return (
    <>
      <Select
        label="Status"
        value={status}
        options={STATUSES.map((s) => ({ value: s, label: s }))}
        onChange={(event) => {
          const value = event.target.value;
          onChange('status', value === 'active' ? undefined : value);
        }}
      />
      <Input
        label={`${what} description`}
        className="sm:col-span-2"
        value={description}
        onChange={(event) => {
          const value = event.target.value;
          onChange('description', value === '' ? undefined : value);
        }}
      />
    </>
  );
}
