import { useState } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select, Textarea } from '@/ui/field';
import { modeler as modelerMessages } from '@/messages/modeler';
import { FormAction, FormRow, Section } from '@/ui/layout';
import { AttributesEditor } from './attributes-editor';
import type { KindDocument, SchemaDocument } from './document';
import {
  addKind,
  errorAt,
  removeKey,
  renameAttribute,
  renameKind,
  type Edit,
  type ElementIds,
} from './form-helpers';

interface Props {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

const STATUSES = ['draft', 'active', 'deprecated'];
const t = modelerMessages.kinds;
const e = modelerMessages.elements;

export function NewName({
  label,
  button,
  taken,
  onAdd,
}: {
  label: string;
  button: string;
  taken: (name: string) => boolean;
  onAdd: (name: string) => void;
}) {
  const [name, setName] = useState('');
  const problem = name !== '' && taken(name) ? e.exists(name) : undefined;
  return (
    <FormRow>
      <Input
        label={label}
        value={name}
        error={problem}
        onChange={(event) => {
          setName(event.target.value.trim());
        }}
      />
      <FormAction>
        <Button
          disabled={name === '' || problem !== undefined}
          onClick={() => {
            onAdd(name);
            setName('');
          }}
        >
          {button}
        </Button>
      </FormAction>
    </FormRow>
  );
}

function KindName({
  name,
  issues,
  document,
  edit,
}: {
  name: string;
  issues: SchemaIssue[];
  document: SchemaDocument;
  edit: Edit;
}) {
  const [typed, setTyped] = useState<string | null>(null);
  const taken = typed !== null && typed !== name && typed in (document.kinds ?? {});
  return (
    <Input
      label={t.kindName}
      value={typed ?? name}
      error={taken ? e.exists(typed) : errorAt(issues, `kinds/${name}`)}
      onChange={(event) => {
        const next = event.target.value;
        setTyped(next);
        if (next !== '' && !(next in (document.kinds ?? {}))) {
          edit((draft) => {
            renameKind(draft, name, next);
          });
        }
      }}
      onBlur={() => {
        setTyped(null);
      }}
    />
  );
}

/** `search.embedding_template` and `search.embedding_sensitive`; empty values leave the keys out. */
function SemanticSearch({
  kind,
  kindName,
  issues,
  edit,
}: {
  kind: KindDocument;
  kindName: string;
  issues: SchemaIssue[];
  edit: Edit;
}) {
  const search = kind.search ?? {};
  const update = (change: (target: NonNullable<KindDocument['search']>) => void) => {
    edit((draft) => {
      const target = draft.kinds?.[kindName];
      if (target === undefined) return;
      const next = { ...target.search };
      change(next);
      if (Object.keys(next).length === 0) delete target.search;
      else target.search = next;
    });
  };
  return (
    <Section title={t.search}>
      <div className="flex flex-col gap-2">
        <Textarea
          label={t.embeddingTemplate}
          hint={t.embeddingTemplateHint}
          value={search.embedding_template ?? ''}
          error={errorAt(issues, `kinds/${kindName}/search/embedding_template`)}
          onChange={(event) => {
            const value = event.target.value;
            update((next) => {
              if (value.trim() === '') delete next.embedding_template;
              else next.embedding_template = value;
            });
          }}
        />
        <label className="flex items-center gap-1.5 text-sm">
          <input
            type="checkbox"
            checked={search.embedding_sensitive === true}
            onChange={(event) => {
              const on = event.target.checked;
              update((next) => {
                if (on) next.embedding_sensitive = true;
                else delete next.embedding_sensitive;
              });
            }}
          />
          {t.embeddingSensitive}
        </label>
      </div>
    </Section>
  );
}

function TraitMapping({
  kind,
  kindName,
  document,
  issues,
  edit,
}: {
  kind: KindDocument;
  kindName: string;
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
}) {
  const traits = document.traits ?? {};
  const implemented = new Set((kind.traits ?? []).map((t) => t.trait));
  const available = Object.keys(traits).filter((name) => !implemented.has(name));
  const attributeNames = Object.keys(kind.attributes ?? {});
  const [adding, setAdding] = useState('');
  const choice = adding !== '' && available.includes(adding) ? adding : (available[0] ?? '');
  return (
    <Section title={t.traits}>
      <div className="flex flex-col gap-3">
        {(kind.traits ?? []).map((implementation, index) => (
          <div
            key={implementation.trait}
            className="rounded-md border border-line bg-surface p-3"
            role="group"
            aria-label={t.traitGroup(implementation.trait)}
          >
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-medium">{implementation.trait}</h4>
              <Button
                size="sm"
                variant="ghost"
                aria-label={t.removeTrait(implementation.trait)}
                onClick={() => {
                  edit((draft) => {
                    const target = draft.kinds?.[kindName];
                    if (target !== undefined) {
                      target.traits = (target.traits ?? []).filter((_, i) => i !== index);
                    }
                  });
                }}
              >
                {t.remove}
              </Button>
            </div>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {Object.keys(traits[implementation.trait]?.attributes ?? {}).map((traitAttribute) => (
                <Select
                  key={traitAttribute}
                  label={t.mapsTo(implementation.trait, traitAttribute)}
                  value={implementation.attribute_map[traitAttribute] ?? ''}
                  error={errorAt(issues, `kinds/${kindName}/traits/${index}/attribute_map`)}
                  options={[
                    { value: '', label: t.notMapped },
                    ...attributeNames.map((n) => ({ value: n, label: n })),
                  ]}
                  onChange={(event) => {
                    const value = event.target.value;
                    edit((draft) => {
                      const map = draft.kinds?.[kindName]?.traits?.[index]?.attribute_map;
                      if (map === undefined) return;
                      if (value === '') delete map[traitAttribute];
                      else map[traitAttribute] = value;
                    });
                  }}
                />
              ))}
            </div>
          </div>
        ))}
        {available.length > 0 && (
          <FormRow>
            <Select
              label={t.traitToImplement}
              value={choice}
              options={available.map((n) => ({ value: n, label: n }))}
              onChange={(event) => {
                setAdding(event.target.value);
              }}
            />
            <FormAction>
              <Button
                onClick={() => {
                  edit((draft) => {
                    const target = draft.kinds?.[kindName];
                    if (target === undefined) return;
                    target.traits = [
                      ...(target.traits ?? []),
                      { trait: choice, attribute_map: {} },
                    ];
                  });
                }}
              >
                {t.implementTrait}
              </Button>
            </FormAction>
          </FormRow>
        )}
      </div>
    </Section>
  );
}

/** Kind editor: identity, key and title, Attributes with types and constraints, Traits. */
export function KindsForm({ document, issues, edit, ids, selected, onSelect }: Props) {
  const kinds = document.kinds ?? {};
  const names = Object.keys(kinds);
  const name = selected !== undefined && selected in kinds ? selected : names[0];
  const kind = name === undefined ? undefined : kinds[name];

  return (
    <div className="flex flex-col gap-4">
      <FormRow>
        {name !== undefined && (
          <Select
            label={t.kind}
            value={name}
            options={names.map((n) => ({ value: n, label: n }))}
            onChange={(event) => {
              onSelect(event.target.value);
            }}
          />
        )}
        <NewName
          label={t.newKind}
          button={t.addKind}
          taken={(candidate) => candidate in kinds}
          onAdd={(added) => {
            edit((draft) => {
              addKind(draft, added, ids);
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
                  draft.kinds = removeKey(draft.kinds, name);
                });
                onSelect(undefined);
              }}
            >
              {t.removeKind}
            </Button>
          </FormAction>
        )}
      </FormRow>

      {name === undefined || kind === undefined ? null : (
        <div className="flex flex-col gap-4" key={kind.id ?? name}>
          <div className="grid gap-3 sm:grid-cols-2">
            <KindName name={name} issues={issues} document={document} edit={edit} />
            <Select
              label={e.status}
              value={kind.status ?? 'active'}
              options={STATUSES.map((s) => ({ value: s, label: s }))}
              onChange={(event) => {
                const status = event.target.value;
                edit((draft) => {
                  const target = draft.kinds?.[name];
                  if (target === undefined) return;
                  if (status === 'active') delete target.status;
                  else target.status = status;
                });
              }}
            />
            <Input
              label={t.kindDescription}
              className="sm:col-span-2"
              value={kind.description ?? ''}
              onChange={(event) => {
                const value = event.target.value;
                edit((draft) => {
                  const target = draft.kinds?.[name];
                  if (target === undefined) return;
                  if (value === '') delete target.description;
                  else target.description = value;
                });
              }}
            />
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-xs font-medium text-muted">{t.primaryKey}</legend>
              {Object.keys(kind.attributes ?? {}).map((attribute) => (
                <label key={attribute} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={(kind.primary_key ?? []).includes(attribute)}
                    onChange={(event) => {
                      const on = event.target.checked;
                      edit((draft) => {
                        const target = draft.kinds?.[name];
                        if (target === undefined) return;
                        const rest = (target.primary_key ?? []).filter((n) => n !== attribute);
                        target.primary_key = on ? [...rest, attribute] : rest;
                      });
                    }}
                  />
                  {t.primaryKeyOf(attribute)}
                </label>
              ))}
              {errorAt(issues, `kinds/${name}/primary_key`) !== undefined && (
                <p className="text-xs text-danger">
                  {errorAt(issues, `kinds/${name}/primary_key`)}
                </p>
              )}
            </fieldset>
            <Select
              label={t.titleAttribute}
              value={kind.title_attribute ?? ''}
              error={errorAt(issues, `kinds/${name}/title_attribute`)}
              options={Object.keys(kind.attributes ?? {}).map((n) => ({ value: n, label: n }))}
              onChange={(event) => {
                const value = event.target.value;
                edit((draft) => {
                  const target = draft.kinds?.[name];
                  if (target !== undefined) target.title_attribute = value;
                });
              }}
            />
          </div>

          <Section title={t.attributes}>
            <AttributesEditor
              attributes={kind.attributes ?? {}}
              path={`kinds/${name}/attributes`}
              issues={issues}
              kindNames={Object.keys(kinds)}
              ids={ids}
              onChange={(next) => {
                edit((draft) => {
                  const target = draft.kinds?.[name];
                  if (target !== undefined) target.attributes = next;
                });
              }}
              onRename={(from, to) => {
                edit((draft) => {
                  renameAttribute(draft, name, from, to);
                });
              }}
            />
          </Section>

          <SemanticSearch kind={kind} kindName={name} issues={issues} edit={edit} />

          <TraitMapping
            kind={kind}
            kindName={name}
            document={document}
            issues={issues}
            edit={edit}
          />
        </div>
      )}
    </div>
  );
}
