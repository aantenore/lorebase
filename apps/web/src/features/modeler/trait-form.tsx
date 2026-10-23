import { useState } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { modeler as modelerMessages } from '@/messages/modeler';
import { FormAction, FormRow, Section } from '@/ui/layout';
import type { SchemaDocument, TraitDocument } from './document';
import {
  addTrait,
  errorAt,
  newAttribute,
  removeKey,
  renameKey,
  typeFromLabel,
  typeLabel,
  typeOptions,
  type Edit,
  type ElementIds,
} from './form-helpers';
import { NewName } from './kind-form';

const t = modelerMessages.traits;

interface Props {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

function implementers(document: SchemaDocument, trait: string): string[] {
  return Object.entries(document.kinds ?? {})
    .filter(([, kind]) => (kind.traits ?? []).some((t) => t.trait === trait))
    .map(([name]) => name);
}

/** Trait editor: Attributes (name, type, required) and the Kinds that implement the Trait. */
export function TraitsForm({ document, issues, edit, ids, selected, onSelect }: Props) {
  const traits = document.traits ?? {};
  const names = Object.keys(traits);
  const name = selected !== undefined && selected in traits ? selected : names[0];
  const trait: TraitDocument | undefined = name === undefined ? undefined : traits[name];
  const [typed, setTyped] = useState<string | null>(null);

  const change = (apply: (target: TraitDocument) => void) => {
    if (name === undefined) return;
    edit((draft) => {
      const target = draft.traits?.[name];
      if (target !== undefined) apply(target);
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <FormRow>
        {name !== undefined && (
          <Select
            label={t.trait}
            value={name}
            options={names.map((n) => ({ value: n, label: n }))}
            onChange={(event) => {
              onSelect(event.target.value);
            }}
          />
        )}
        <NewName
          label={t.newTrait}
          button={t.addTrait}
          taken={(candidate) => candidate in traits}
          onAdd={(added) => {
            edit((draft) => {
              addTrait(draft, added, ids);
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
                  draft.traits = removeKey(draft.traits, name);
                });
                onSelect(undefined);
              }}
            >
              {t.removeTrait}
            </Button>
          </FormAction>
        )}
      </FormRow>

      {name === undefined || trait === undefined ? null : (
        <div className="flex flex-col gap-4" key={trait.id ?? name}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Input
              label={t.traitName}
              value={typed ?? name}
              error={errorAt(issues, `traits/${name}`)}
              onChange={(event) => {
                const next = event.target.value;
                setTyped(next);
                if (next !== '' && !(next in traits)) {
                  edit((draft) => {
                    draft.traits = renameKey(draft.traits ?? {}, name, next);
                    for (const kind of Object.values(draft.kinds ?? {})) {
                      for (const t of kind.traits ?? []) if (t.trait === name) t.trait = next;
                    }
                  });
                }
              }}
              onBlur={() => {
                setTyped(null);
              }}
            />
            <Input
              label={t.traitDescription}
              value={trait.description ?? ''}
              onChange={(event) => {
                const value = event.target.value;
                change((target) => {
                  if (value === '') delete target.description;
                  else target.description = value;
                });
              }}
            />
          </div>

          <Section title={t.attributes}>
            <ul className="flex flex-col gap-2" aria-label={t.attributes}>
              {Object.entries(trait.attributes ?? {}).map(([attribute, definition]) => (
                <li
                  key={definition.id ?? attribute}
                  className="grid gap-2 rounded-md border border-line bg-surface p-3 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-start"
                >
                  <Input
                    label={t.nameOf(attribute)}
                    value={attribute}
                    error={errorAt(issues, `traits/${name}/attributes/${attribute}`)}
                    onChange={(event) => {
                      const next = event.target.value;
                      if (next === '' || next in (trait.attributes ?? {})) return;
                      change((target) => {
                        target.attributes = renameKey(target.attributes ?? {}, attribute, next);
                      });
                    }}
                  />
                  <Select
                    label={t.typeOf(attribute)}
                    value={typeLabel(definition.type)}
                    error={errorAt(issues, `traits/${name}/attributes/${attribute}/type`)}
                    options={typeOptions([], typeLabel(definition.type)).map((v) => ({
                      value: v,
                      label: v,
                    }))}
                    onChange={(event) => {
                      const type = typeFromLabel(event.target.value, []);
                      change((target) => {
                        const own = target.attributes?.[attribute];
                        if (own !== undefined) own.type = type;
                      });
                    }}
                  />
                  <label className="flex items-center gap-1.5 text-sm sm:mt-6">
                    <input
                      type="checkbox"
                      checked={definition.required === true}
                      onChange={(event) => {
                        const on = event.target.checked;
                        change((target) => {
                          const own = target.attributes?.[attribute];
                          if (own === undefined) return;
                          if (on) own.required = true;
                          else delete own.required;
                        });
                      }}
                    />
                    {t.requiredOf(attribute)}
                  </label>
                  <Button
                    variant="ghost"
                    className="sm:mt-5"
                    aria-label={t.removeNamed(attribute)}
                    onClick={() => {
                      change((target) => {
                        target.attributes = removeKey(target.attributes, attribute);
                      });
                    }}
                  >
                    {t.remove}
                  </Button>
                </li>
              ))}
            </ul>
            <div className="mt-2">
              <Button
                size="sm"
                onClick={() => {
                  change((target) => {
                    const [added, definition] = newAttribute(
                      target.attributes,
                      ids,
                      `traits/${name}/attributes`,
                    );
                    target.attributes = {
                      ...target.attributes,
                      [added]: { id: definition.id, type: 'string' },
                    };
                  });
                }}
              >
                {t.addAttribute}
              </Button>
            </div>
          </Section>

          <Section title={t.implementedBy}>
            <p className="text-sm text-muted">
              {implementers(document, name).join(', ') || t.noImplementers}
            </p>
          </Section>
        </div>
      )}
    </div>
  );
}
