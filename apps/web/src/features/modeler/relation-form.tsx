import { useState } from 'react';
import { CARDINALITIES, ON_DELETE, type SchemaIssue } from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { modeler as modelerMessages } from '@/messages/modeler';
import { FormAction, FormRow, Section } from '@/ui/layout';
import { AttributesEditor } from './attributes-editor';
import type { RelationDocument, SchemaDocument } from './document';
import {
  addRelation,
  errorAt,
  removeKey,
  renameKey,
  type Edit,
  type ElementIds,
} from './form-helpers';
import { NewName } from './kind-form';

const t = modelerMessages.relations;

interface Props {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

function RelationName({
  name,
  document,
  issues,
  edit,
}: {
  name: string;
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const taken = typed !== null && typed !== name && typed in (document.relations ?? {});
  return (
    <Input
      label={t.relationName}
      value={typed ?? name}
      error={taken ? modelerMessages.elements.exists(typed) : errorAt(issues, `relations/${name}`)}
      onChange={(event) => {
        const next = event.target.value;
        setTyped(next);
        if (next !== '' && !(next in (document.relations ?? {}))) {
          edit((draft) => {
            draft.relations = renameKey(draft.relations ?? {}, name, next);
          });
        }
      }}
      onBlur={() => {
        setTyped(null);
      }}
    />
  );
}

/** Relation editor: endpoints, cardinality, required, delete behavior and link Attributes. */
export function RelationsForm({ document, issues, edit, ids, selected, onSelect }: Props) {
  const relations = document.relations ?? {};
  const names = Object.keys(relations);
  const name = selected !== undefined && selected in relations ? selected : names[0];
  const relation: RelationDocument | undefined = name === undefined ? undefined : relations[name];
  const kindNames = Object.keys(document.kinds ?? {});
  const set = (patch: Partial<RelationDocument>) => {
    if (name === undefined) return;
    edit((draft) => {
      const target = draft.relations?.[name];
      if (target === undefined) return;
      const next: Record<string, unknown> = { ...target, ...patch };
      for (const key of Object.keys(next)) {
        if (next[key] === undefined || next[key] === false) delete next[key];
      }
      draft.relations = { ...draft.relations, [name]: next as unknown as RelationDocument };
    });
  };
  const kindOptions = (current: string) =>
    (kindNames.includes(current) ? kindNames : [current, ...kindNames]).map((n) => ({
      value: n,
      label: n === '' ? t.chooseKind : n,
    }));

  return (
    <div className="flex flex-col gap-4">
      <FormRow>
        {name !== undefined && (
          <Select
            label={t.relation}
            value={name}
            options={names.map((n) => ({ value: n, label: n }))}
            onChange={(event) => {
              onSelect(event.target.value);
            }}
          />
        )}
        <NewName
          label={t.newRelation}
          button={t.addRelation}
          taken={(candidate) => candidate in relations}
          onAdd={(added) => {
            edit((draft) => {
              addRelation(draft, added, ids);
            });
            onSelect(added);
          }}
        />
        {name !== undefined && (
          <FormAction>
            <Button
              variant="danger"
              onClick={() => {
                edit((draft) => {
                  draft.relations = removeKey(draft.relations, name);
                });
                onSelect(undefined);
              }}
            >
              {t.removeRelation}
            </Button>
          </FormAction>
        )}
      </FormRow>

      {name === undefined || relation === undefined ? null : (
        <div className="flex flex-col gap-4" key={relation.id ?? name}>
          <div className="grid gap-3 sm:grid-cols-2">
            <RelationName name={name} document={document} issues={issues} edit={edit} />
            <Input
              label={t.inverseName}
              value={relation.inverse_api_name}
              error={errorAt(issues, `relations/${name}/inverse_api_name`)}
              onChange={(event) => {
                set({ inverse_api_name: event.target.value });
              }}
            />
            <Select
              label={t.fromKind}
              value={relation.from_kind}
              error={errorAt(issues, `relations/${name}/from_kind`)}
              options={kindOptions(relation.from_kind)}
              onChange={(event) => {
                set({ from_kind: event.target.value });
              }}
            />
            <Select
              label={t.toKind}
              value={relation.to_kind}
              error={errorAt(issues, `relations/${name}/to_kind`)}
              options={kindOptions(relation.to_kind)}
              onChange={(event) => {
                set({ to_kind: event.target.value });
              }}
            />
            <Select
              label={t.cardinality}
              value={relation.cardinality ?? 'many_to_many'}
              error={errorAt(issues, `relations/${name}/cardinality`)}
              options={CARDINALITIES.map((c) => ({ value: c, label: c }))}
              onChange={(event) => {
                set({ cardinality: event.target.value });
              }}
            />
            <Select
              label={t.onDelete}
              value={relation.on_delete ?? 'cascade'}
              error={errorAt(issues, `relations/${name}/on_delete`)}
              options={ON_DELETE.map((c) => ({ value: c, label: c }))}
              onChange={(event) => {
                set({
                  on_delete: event.target.value === 'cascade' ? undefined : event.target.value,
                });
              }}
            />
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={relation.required_from === true}
                onChange={(event) => {
                  set({ required_from: event.target.checked });
                }}
              />
              {t.requiredFrom}
            </label>
            <Input
              label={t.description}
              value={relation.description ?? ''}
              onChange={(event) => {
                set({ description: event.target.value === '' ? undefined : event.target.value });
              }}
            />
          </div>
          <Section title={t.linkAttributes}>
            <AttributesEditor
              attributes={relation.attributes ?? {}}
              path={`relations/${name}/attributes`}
              issues={issues}
              kindNames={kindNames}
              ids={ids}
              onChange={(next) => {
                set({ attributes: Object.keys(next).length === 0 ? undefined : next });
              }}
              onRename={(from, to) => {
                set({ attributes: renameKey(relation.attributes ?? {}, from, to) });
              }}
            />
          </Section>
        </div>
      )}
    </div>
  );
}
