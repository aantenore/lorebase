import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
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
      <legend className="mb-1 text-xs font-medium text-muted">{label} value table</legend>
      {entries.map(([from, to], index) => {
        const n = index + 1;
        return (
          <div key={index} className="flex flex-wrap items-start gap-2">
            <KeyInput
              label={`${label} map ${n} from`}
              name={from}
              taken={(candidate) => candidate !== from && candidate in values}
              onRename={(next) => {
                update((a) => {
                  a['values'] = renameKey(values, from, next);
                });
              }}
            />
            <ScalarInput
              label={`${label} map ${n} to`}
              value={to}
              onChange={(next) => {
                update((a) => {
                  (a['values'] as Doc)[from] = next;
                });
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              className="mt-5"
              aria-label={`Remove ${label} map ${n}`}
              onClick={() => {
                update((a) => {
                  delete (a['values'] as Doc)[from];
                });
              }}
            >
              Remove
            </Button>
          </div>
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
          Add value
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
          label={`${label} target type`}
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
          label={`${label} date format`}
          hint="A strptime format such as %Y-%m-%d."
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
            label={`${label} columns`}
            hint="Columns appended to the value, comma separated."
            value={asList(args['columns']).map(asText)}
            onChange={(next) => {
              update((a) => {
                a['columns'] = next;
              });
            }}
          />
          <Input
            label={`${label} separator`}
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
          label={`${label} constant`}
          hint="Replaces the value."
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
          label={`${label} values`}
          hint="The value becomes empty when it is one of these, comma separated."
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
            label={`${label} default`}
            hint="Used for values not in the table; empty keeps the value."
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
      <legend className="mb-1 text-xs font-medium text-muted">Transforms of {attribute}</legend>
      {transforms.length === 0 ? (
        <p className="text-xs text-muted">None: the value is used as it is.</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {transforms.map((transform, index) => {
            const n = index + 1;
            const label = `${attribute} transform ${n}`;
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
                  label={`${label} name`}
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
                <div className="flex items-end gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Move ${label} up`}
                    disabled={index === 0}
                    onClick={() => {
                      change((list) => moveItem(list, index, index - 1));
                    }}
                  >
                    Up
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Move ${label} down`}
                    disabled={index === transforms.length - 1}
                    onClick={() => {
                      change((list) => moveItem(list, index, index + 1));
                    }}
                  >
                    Down
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove ${label}`}
                    onClick={() => {
                      change((list) => list.filter((_, i) => i !== index));
                    }}
                  >
                    Remove
                  </Button>
                </div>
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
          Add transform to {attribute}
        </Button>
      </div>
    </fieldset>
  );
}
