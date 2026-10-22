import { AGGREGATE_FUNCTIONS, LINK_DIRECTIONS, type SchemaIssue } from '@/api/schema-rules';
import type { DerivationVariant } from '@/api/types';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Section } from '@/ui/layout';
import { CelField } from './cel-field';
import { DerivationPreview } from './derivation-preview';
import type { SchemaDocument } from './document';
import { ElementName, ElementPicker, StatusAndDescription } from './element-controls';
import {
  addDerivation,
  changeVariant,
  definitionOf,
  removeDerivation,
  renameDerivation,
  setDerivationTarget,
  setOrDelete,
} from './element-edits';
import {
  asList,
  asText,
  errorAt,
  errorsAt,
  isRecord,
  type Edit,
  type ElementIds,
} from './form-helpers';

type Doc = Record<string, unknown>;

interface Props {
  document: SchemaDocument;
  /** Whether the named Derivation is saved on the Branch exactly as shown. */
  saved?: (name: string) => boolean;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

const VARIANTS: { value: DerivationVariant; label: string }[] = [
  { value: 'expression', label: 'expression: CEL over the Record' },
  { value: 'aggregate', label: 'aggregate: over linked Records' },
  { value: 'path', label: 'path: a composed Relation' },
];

const options = (values: readonly string[], current: string, empty?: string) => [
  ...(empty === undefined ? [] : [{ value: '', label: empty }]),
  ...(current === '' || values.includes(current) ? [] : [{ value: current, label: current }]),
  ...values.map((v) => ({ value: v, label: v })),
];

/** The Kind at the far end of `relation` when walked in `direction` from `kind`. */
function farKind(document: SchemaDocument, relation: string, direction: string): string {
  const found = document.relations?.[relation];
  if (found === undefined) return '';
  return direction === 'inverse' ? found.from_kind : found.to_kind;
}

function Target({
  name,
  definition,
  document,
  issues,
  at,
  edit,
}: {
  name: string;
  definition: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  at: string;
  edit: Edit;
}) {
  const kinds = Object.keys(document.kinds ?? {});
  const kind = asText(definition['kind']);
  const attribute = asText(definition['attribute']);
  const attributes = Object.keys(document.kinds?.[kind]?.attributes ?? {});
  return (
    <>
      <Select
        label="Target Kind"
        value={kind}
        error={errorAt(issues, `${at}/kind`)}
        options={options(kinds, kind)}
        onChange={(event) => {
          const value = event.target.value;
          edit((draft) => {
            setDerivationTarget(draft, name, value, '');
          });
        }}
      />
      <Select
        label="Target Attribute"
        hint="The Attribute the Derivation computes; it is marked as derived."
        value={attribute}
        error={errorAt(issues, `${at}/attribute`)}
        options={options(attributes, attribute, 'Choose an Attribute')}
        onChange={(event) => {
          const value = event.target.value;
          edit((draft) => {
            setDerivationTarget(draft, name, kind, value);
          });
        }}
      />
    </>
  );
}

function Aggregate({
  definition,
  document,
  issues,
  at,
  change,
}: {
  definition: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  at: string;
  change: (mutate: (definition: Doc) => void) => void;
}) {
  const kind = asText(definition['kind']);
  const relation = asText(definition['relation']);
  const direction = asText(definition['direction']) || 'forward';
  const fn = asText(definition['function']);
  const sourceAttribute = asText(definition['source_attribute']);
  const touching = Object.entries(document.relations ?? {})
    .filter(([, r]) => r.from_kind === kind || r.to_kind === kind)
    .map(([n]) => n);
  const far = farKind(document, relation, direction);
  const farAttributes = Object.keys(document.kinds?.[far]?.attributes ?? {});
  const set = (key: string, value: string | undefined) => {
    change((d) => {
      setOrDelete(d, key, value);
    });
  };
  return (
    <>
      <Select
        label="Relation"
        value={relation}
        error={errorAt(issues, `${at}/relation`)}
        options={options(touching, relation)}
        onChange={(event) => {
          set('relation', event.target.value);
        }}
      />
      <Select
        label="Direction"
        value={direction}
        error={errorAt(issues, `${at}/direction`)}
        options={LINK_DIRECTIONS.map((d) => ({ value: d, label: d }))}
        onChange={(event) => {
          const value = event.target.value;
          set('direction', value === 'forward' ? undefined : value);
        }}
      />
      <Select
        label="Function"
        value={fn}
        error={errorAt(issues, `${at}/function`)}
        options={options(AGGREGATE_FUNCTIONS, fn)}
        onChange={(event) => {
          set('function', event.target.value);
        }}
      />
      <Select
        label="Source Attribute"
        hint={far === '' ? undefined : `An Attribute of ${far}; count needs none.`}
        value={sourceAttribute}
        error={errorAt(issues, `${at}/source_attribute`)}
        options={options(farAttributes, sourceAttribute, 'None')}
        onChange={(event) => {
          set('source_attribute', event.target.value);
        }}
      />
      <CelField
        className="sm:col-span-2"
        label="Filter"
        hint="Optional CEL over each linked Record; only Records where it is true count."
        value={asText(definition['filter'])}
        error={errorAt(issues, `${at}/filter`)}
        onChange={(text) => {
          set('filter', text);
        }}
      />
    </>
  );
}

function PathSteps({
  definition,
  document,
  issues,
  at,
  change,
}: {
  definition: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  at: string;
  change: (mutate: (definition: Doc) => void) => void;
}) {
  const steps = asList(definition['steps']).filter(isRecord);
  const relations = Object.keys(document.relations ?? {});
  const update = (index: number, key: string, value: string | undefined) => {
    change((d) => {
      const step = asList(d['steps'])[index];
      if (isRecord(step)) setOrDelete(step, key, value);
    });
  };
  return (
    <div className="flex flex-col gap-3 sm:col-span-2">
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-muted">Path steps</legend>
        {errorAt(issues, `${at}/steps`) !== undefined && (
          <p className="text-xs text-danger">{errorAt(issues, `${at}/steps`)}</p>
        )}
        <ol className="flex flex-col gap-2">
          {steps.map((step, index) => {
            const n = index + 1;
            const relation = asText(step['relation']);
            return (
              <li key={index} className="flex flex-wrap items-end gap-2">
                <Select
                  label={`Path step ${n} Relation`}
                  value={relation}
                  error={errorsAt(issues, `${at}/steps/${index}/relation`, `${at}/steps/${index}`)}
                  options={options(relations, relation)}
                  onChange={(event) => {
                    update(index, 'relation', event.target.value);
                  }}
                />
                <Select
                  label={`Path step ${n} direction`}
                  value={asText(step['direction']) || 'forward'}
                  options={LINK_DIRECTIONS.map((d) => ({ value: d, label: d }))}
                  onChange={(event) => {
                    const value = event.target.value;
                    update(index, 'direction', value === 'forward' ? undefined : value);
                  }}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove path step ${n}`}
                  onClick={() => {
                    change((d) => {
                      d['steps'] = asList(d['steps']).filter((_, i) => i !== index);
                    });
                  }}
                >
                  Remove
                </Button>
              </li>
            );
          })}
        </ol>
        <div>
          <Button
            onClick={() => {
              change((d) => {
                d['steps'] = [...asList(d['steps']), { relation: relations[0] ?? '' }];
              });
            }}
          >
            Add path step
          </Button>
        </div>
      </fieldset>
      <div className="grid gap-3 sm:grid-cols-2">
        <Input
          label="Inverse name"
          hint="Name of the composed Relation read backwards; optional."
          value={asText(definition['inverse_api_name'])}
          error={errorAt(issues, `${at}/inverse_api_name`)}
          onChange={(event) => {
            const value = event.target.value;
            change((d) => {
              setOrDelete(d, 'inverse_api_name', value);
            });
          }}
        />
        <label className="flex items-center gap-1.5 text-sm sm:mt-5">
          <input
            type="checkbox"
            checked={definition['materialized'] === true}
            onChange={(event) => {
              const on = event.target.checked;
              change((d) => {
                if (on) d['materialized'] = true;
                else delete d['materialized'];
              });
            }}
          />
          Materialized
        </label>
      </div>
    </div>
  );
}

function DerivationEditor({
  name,
  derivation,
  document,
  issues,
  edit,
  onSelect,
  canPreview,
}: {
  name: string;
  derivation: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  onSelect: (name: string | undefined) => void;
  canPreview: boolean;
}) {
  const path = `derivations/${name}`;
  const at = `${path}/definition`;
  const definition = isRecord(derivation['definition']) ? derivation['definition'] : {};
  const variant = (asText(definition['variant']) || 'expression') as DerivationVariant;
  const changeDefinition = (mutate: (definition: Doc) => void) => {
    edit((draft) => {
      const target = draft.derivations?.[name];
      if (target !== undefined) mutate(definitionOf(target));
    });
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <ElementName
          what="Derivation"
          name={name}
          names={Object.keys(document.derivations ?? {})}
          issues={issues}
          path={path}
          onRename={(from, to) => {
            edit((draft) => {
              renameDerivation(draft, from, to);
            });
          }}
          onRenamed={onSelect}
        />
        <StatusAndDescription
          what="Derivation"
          element={derivation}
          onChange={(key, value) => {
            edit((draft) => {
              const target = draft.derivations?.[name];
              if (target !== undefined) setOrDelete(target, key, value);
            });
          }}
        />
      </div>
      <Section title="Definition">
        <div className="grid gap-3 sm:grid-cols-2">
          <Select
            label="Variant"
            className="sm:col-span-2"
            value={variant}
            error={errorAt(issues, `${at}/variant`)}
            options={VARIANTS}
            onChange={(event) => {
              const next = event.target.value as DerivationVariant;
              edit((draft) => {
                changeVariant(draft, name, next);
              });
            }}
          />
          {variant !== 'path' && (
            <Target
              name={name}
              definition={definition}
              document={document}
              issues={issues}
              at={at}
              edit={edit}
            />
          )}
          {variant === 'expression' && (
            <CelField
              className="sm:col-span-2"
              label="Expression"
              hint="CEL over the Attributes of the Record; its type matches the target Attribute."
              value={asText(definition['expression'])}
              error={errorsAt(issues, `${at}/expression`, at)}
              placeholder="supplier_count == 1"
              onChange={(text) => {
                changeDefinition((d) => {
                  d['expression'] = text;
                });
              }}
            />
          )}
          {variant === 'aggregate' && (
            <Aggregate
              definition={definition}
              document={document}
              issues={issues}
              at={at}
              change={changeDefinition}
            />
          )}
          {variant === 'path' && (
            <PathSteps
              definition={definition}
              document={document}
              issues={issues}
              at={at}
              change={changeDefinition}
            />
          )}
        </div>
      </Section>
      <Section title="Preview">
        {asText(definition['kind']) === '' || asText(definition['attribute']) === '' ? (
          <p className="text-sm text-muted">Choose the target Attribute to preview its values.</p>
        ) : (
          <DerivationPreview
            name={name}
            kind={asText(definition['kind'])}
            attribute={asText(definition['attribute'])}
            document={document}
            saved={canPreview}
          />
        )}
      </Section>
    </div>
  );
}

/** Derivation editor: expression, aggregate and path variants with their target. */
export function DerivationsForm({ document, saved, issues, edit, ids, selected, onSelect }: Props) {
  const derivations = document.derivations ?? {};
  const names = Object.keys(derivations);
  const name = selected !== undefined && selected in derivations ? selected : names[0];
  const derivation = name === undefined ? undefined : derivations[name];
  return (
    <div className="flex flex-col gap-4">
      <ElementPicker
        what="Derivation"
        names={names}
        selected={name}
        onSelect={onSelect}
        onAdd={(added) => {
          edit((draft) => {
            addDerivation(draft, added, ids);
          });
        }}
        onRemove={(removed) => {
          edit((draft) => {
            removeDerivation(draft, removed);
          });
        }}
      />
      {name === undefined || derivation === undefined ? (
        <p className="text-sm text-muted">No Derivations.</p>
      ) : (
        <DerivationEditor
          key={asText(derivation['id']) || name}
          name={name}
          derivation={derivation}
          document={document}
          issues={issues}
          edit={edit}
          onSelect={onSelect}
          canPreview={saved?.(name) ?? false}
        />
      )}
    </div>
  );
}
