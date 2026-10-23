import { modelerEditors as modelerEditorsMessages } from '@/messages/modeler-editors';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { FormAction, FormRow } from '@/ui/layout';
import { asList, asText, isRecord, moveItem, renameKey } from './form-helpers';
import { KeyInput } from './key-input';
import { ListInput } from './list-input';
import { ScalarInput } from './scalar-input';
import {
  argsOf,
  CAST_TARGETS,
  changeTransformName,
  nullIfValues,
  scalarFrom,
  scalarText,
  TRANSFORM_NAMES,
  transformProblem,
} from './transform-edits';

type Doc = Record<string, unknown>;

const t = modelerEditorsMessages.source.transforms;
const actions = modelerEditorsMessages.actions;

/** Applies `mutate` to the transform list of one Attribute mapping, in the document draft. */
export type ChangeTransforms = (mutate: (transforms: Doc[]) => Doc[] | void) => void;

const NO_ARGS = new Set(['trim', 'lower', 'upper']);

function MapTable({
  label,
  transform,
  update,
}: {
  label: string;
  transform: Doc;
  update: (mutate: (args: Doc) => void) => void;
}) {
  const args = isRecord(transform['args']) ? transform['args'] : {};
  const values = isRecord(args['values']) ? args['values'] : {};
  const entries = Object.entries(values);
  return (
    <fieldset className="flex flex-col gap-2 sm:col-span-2">
      <legend className="mb-1 text-xs font-medium text-muted">{t.valueTable(label)}</legend>
      {entries.map(([from, to], index) => {
        const n = index + 1;
        return (
          <FormRow key={index}>
            <KeyInput
              label={t.mapFrom(label, n)}
              name={from}
              taken={(candidate) => candidate !== from && candidate in values}
              onRename={(next) => {
                update((a) => {
                  a['values'] = renameKey(values, from, next);
                });
              }}
            />
            <ScalarInput
              label={t.mapTo(label, n)}
              value={to}
              onChange={(next) => {
                update((a) => {
                  (a['values'] as Doc)[from] = next;
                });
              }}
            />
            <FormAction>
              <Button
                variant="ghost"
                aria-label={t.removeMap(label, n)}
                onClick={() => {
                  update((a) => {
                    delete (a['values'] as Doc)[from];
                  });
                }}
              >
                {actions.remove}
              </Button>
            </FormAction>
          </FormRow>
        );
      })}
      <div>
        <Button
          size="sm"
          onClick={() => {
            update((a) => {
              const next = isRecord(a['values']) ? a['values'] : {};
              let n = entries.length + 1;
              while (`value_${n}` in next) n += 1;
              next[`value_${n}`] = '';
              a['values'] = next;
            });
          }}
        >
          {t.addValue}
        </Button>
      </div>
    </fieldset>
  );
}

/** The arguments of one transform, by name. */
function Arguments({
  label,
  transform,
  update,
}: {
  label: string;
  transform: Doc;
  update: (mutate: (args: Doc) => void) => void;
}) {
  const args = isRecord(transform['args']) ? transform['args'] : {};
  switch (transform['name']) {
    case 'cast':
      return (
        <Select
          label={t.targetType(label)}
          value={asText(args['to'])}
          options={[
            ...(CAST_TARGETS.some((t) => t === args['to'])
              ? []
              : [{ value: asText(args['to']), label: asText(args['to']) || '-' }]),
            ...CAST_TARGETS.map((t) => ({ value: t, label: t })),
          ]}
          onChange={(event) => {
            const value = event.target.value;
            update((a) => {
              a['to'] = value;
            });
          }}
        />
      );
    case 'parse_date':
      return (
        <Input
          label={t.dateFormat(label)}
          hint={t.dateFormatHint}
          value={asText(args['format'])}
          onChange={(event) => {
            const value = event.target.value;
            update((a) => {
              a['format'] = value;
            });
          }}
        />
      );
    case 'concat':
      return (
        <>
          <ListInput
            label={t.columns(label)}
            hint={t.columnsHint}
            value={asList(args['columns']).map(asText)}
            onChange={(next) => {
              update((a) => {
                a['columns'] = next;
              });
            }}
          />
          <Input
            label={t.separator(label)}
            value={asText(args['separator'])}
            onChange={(event) => {
              const value = event.target.value;
              update((a) => {
                if (value === '') delete a['separator'];
                else a['separator'] = value;
              });
            }}
          />
        </>
      );
    case 'constant':
      return (
        <ScalarInput
          label={t.constant(label)}
          hint={t.constantHint}
          value={args['value']}
          onChange={(next) => {
            update((a) => {
              a['value'] = next;
            });
          }}
        />
      );
    case 'null_if':
      return (
        <ListInput
          label={t.values(label)}
          hint={t.valuesHint}
          value={nullIfValues(args).map(scalarText)}
          onChange={(next) => {
            update((a) => {
              const previous = nullIfValues(a);
              a['values'] = next.map((text, index) => scalarFrom(text, previous[index]));
              delete a['value'];
            });
          }}
        />
      );
    case 'map':
      return (
        <>
          <MapTable label={label} transform={transform} update={update} />
          <ScalarInput
            label={t.default(label)}
            hint={t.defaultHint}
            value={args['default']}
            onChange={(next) => {
              update((a) => {
                if (next === '') delete a['default'];
                else a['default'] = next;
              });
            }}
          />
        </>
      );
    default:
      return null;
  }
}

/** The transforms of one Attribute mapping, run in order on the value. */
export function TransformsEditor({
  attribute,
  transforms,
  change,
}: {
  attribute: string;
  transforms: Doc[];
  change: ChangeTransforms;
}) {
  return (
    <fieldset className="flex flex-col gap-2 sm:col-span-2">
      <legend className="mb-1 text-xs font-medium text-muted">{t.title(attribute)}</legend>
      {transforms.length === 0 ? (
        <p className="text-xs text-muted">{t.none}</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {transforms.map((transform, index) => {
            const n = index + 1;
            const label = t.item(attribute, n);
            const name = asText(transform['name']);
            const known = TRANSFORM_NAMES.some((t) => t === name);
            const problem = transformProblem(transform);
            const update = (mutate: (args: Doc) => void) => {
              change((list) => {
                const found = list[index];
                if (found !== undefined) mutate(argsOf(found));
              });
            };
            return (
              <li
                key={index}
                aria-label={label}
                className="grid gap-2 rounded-md border border-line bg-surface p-2 sm:grid-cols-2"
              >
                <Select
                  label={t.name(label)}
                  value={name}
                  error={problem}
                  options={[
                    ...(known ? [] : [{ value: name, label: name || '-' }]),
                    ...TRANSFORM_NAMES.map((t) => ({ value: t, label: t })),
                  ]}
                  onChange={(event) => {
                    const value = event.target.value;
                    change((list) => {
                      const found = list[index];
                      if (found !== undefined) changeTransformName(found, value);
                    });
                  }}
                />
                <FormAction>
                  <Button
                    variant="ghost"
                    aria-label={t.moveUp(label)}
                    disabled={index === 0}
                    onClick={() => {
                      change((list) => moveItem(list, index, index - 1));
                    }}
                  >
                    {actions.up}
                  </Button>
                  <Button
                    variant="ghost"
                    aria-label={t.moveDown(label)}
                    disabled={index === transforms.length - 1}
                    onClick={() => {
                      change((list) => moveItem(list, index, index + 1));
                    }}
                  >
                    {actions.down}
                  </Button>
                  <Button
                    variant="ghost"
                    aria-label={t.remove(label)}
                    onClick={() => {
                      change((list) => list.filter((_, i) => i !== index));
                    }}
                  >
                    {actions.remove}
                  </Button>
                </FormAction>
                {!NO_ARGS.has(name) && (
                  <Arguments label={label} transform={transform} update={update} />
                )}
              </li>
            );
          })}
        </ol>
      )}
      <div>
        <Button
          size="sm"
          onClick={() => {
            change((list) => {
              const added: Doc = {};
              changeTransformName(added, 'trim');
              return [...list, added];
            });
          }}
        >
          {t.add(attribute)}
        </Button>
      </div>
    </fieldset>
  );
}
