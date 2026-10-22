import { useEffect, useRef, useState } from 'react';
import {
  OPERATION_ACTIONS,
  STEP_FIELDS,
  type OperationAction,
  type SchemaIssue,
} from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Section } from '@/ui/layout';
import { CelField } from './cel-field';
import type { SchemaDocument, TypeDocument } from './document';
import { ElementName, ElementPicker, StatusAndDescription } from './element-controls';
import {
  addOperation,
  changeStepAction,
  child,
  moveStep,
  renameOperation,
  reviewPolicyOf,
  setOrDelete,
  setReviewPolicy,
  wholeNumber,
  type ReviewMode,
} from './element-edits';
import {
  asList,
  asText,
  enumValuesOf,
  errorAt,
  errorsAt,
  isRecord,
  removeKey,
  typeFromLabel,
  typeLabel,
  typeOptions,
  type Edit,
  type ElementIds,
} from './form-helpers';
import { ListInput } from './list-input';
import { OperationDryRun } from './operation-dry-run';

type Doc = Record<string, unknown>;

interface Props {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
  /** Whether the Operation is saved on the Branch as shown, so it can be dry run. */
  saved?: (name: string) => boolean;
}

type Change = (mutate: (operation: Doc, draft: SchemaDocument) => void) => void;

function Parameters({
  operation,
  path,
  issues,
  kindNames,
  change,
}: {
  operation: Doc;
  path: string;
  issues: SchemaIssue[];
  kindNames: string[];
  change: Change;
}) {
  const parameters = asList(operation['parameters']).filter(isRecord);
  const update = (index: number, mutate: (parameter: Doc) => void) => {
    change((target) => {
      const parameter = asList(target['parameters'])[index];
      if (isRecord(parameter)) mutate(parameter);
    });
  };
  return (
    <div className="flex flex-col gap-3">
      {parameters.length === 0 && <p className="text-sm text-muted">No parameters.</p>}
      <ol className="flex flex-col gap-3">
        {parameters.map((parameter, index) => {
          const n = index + 1;
          const type = parameter['type'] as TypeDocument | undefined;
          const label = typeLabel(type);
          const isEnum = label === 'enum' || label === 'array<enum>';
          const at = `${path}/parameters/${index}`;
          return (
            <li
              key={index}
              className="grid gap-2 rounded-md border border-line bg-surface p-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-start"
            >
              <Input
                label={`Parameter ${n} name`}
                value={asText(parameter['name'])}
                error={errorsAt(
                  issues,
                  `${at}/name`,
                  `${path}/parameters/${asText(parameter['name'])}`,
                )}
                onChange={(event) => {
                  const value = event.target.value.trim();
                  update(index, (p) => {
                    p['name'] = value;
                  });
                }}
              />
              <Select
                label={`Parameter ${n} type`}
                value={label}
                error={errorAt(issues, `${at}/type`)}
                options={typeOptions(kindNames, label).map((v) => ({ value: v, label: v }))}
                onChange={(event) => {
                  const value = event.target.value;
                  update(index, (p) => {
                    p['type'] = typeFromLabel(value, enumValuesOf(type));
                  });
                }}
              />
              {isEnum ? (
                <ListInput
                  label={`Parameter ${n} enum values`}
                  value={enumValuesOf(type)}
                  onChange={(values) => {
                    update(index, (p) => {
                      p['type'] = typeFromLabel(label, values);
                    });
                  }}
                />
              ) : (
                <Input
                  label={`Parameter ${n} description`}
                  value={asText(parameter['description'])}
                  onChange={(event) => {
                    const value = event.target.value;
                    update(index, (p) => {
                      setOrDelete(p, 'description', value);
                    });
                  }}
                />
              )}
              <div className="flex items-center gap-3 sm:mt-5">
                <label className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={parameter['required'] === true}
                    onChange={(event) => {
                      const on = event.target.checked;
                      update(index, (p) => {
                        if (on) p['required'] = true;
                        else delete p['required'];
                      });
                    }}
                  />
                  Parameter {n} required
                </label>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove parameter ${n}`}
                  onClick={() => {
                    change((target) => {
                      target['parameters'] = asList(target['parameters']).filter(
                        (_, i) => i !== index,
                      );
                    });
                  }}
                >
                  Remove
                </Button>
              </div>
            </li>
          );
        })}
      </ol>
      <div>
        <Button
          onClick={() => {
            change((target) => {
              const taken = new Set(
                asList(target['parameters']).map((p) => (isRecord(p) ? p['name'] : undefined)),
              );
              let n = parameters.length + 1;
              while (taken.has(`parameter_${n}`)) n += 1;
              target['parameters'] = [
                ...asList(target['parameters']),
                { name: `parameter_${n}`, type: 'string' },
              ];
            });
          }}
        >
          Add parameter
        </Button>
      </div>
    </div>
  );
}

function Preconditions({
  operation,
  path,
  issues,
  change,
}: {
  operation: Doc;
  path: string;
  issues: SchemaIssue[];
  change: Change;
}) {
  const preconditions = asList(operation['preconditions']).filter(isRecord);
  const update = (index: number, key: string, value: string) => {
    change((target) => {
      const precondition = asList(target['preconditions'])[index];
      if (isRecord(precondition)) precondition[key] = value;
    });
  };
  return (
    <div className="flex flex-col gap-3">
      {preconditions.length === 0 && <p className="text-sm text-muted">No preconditions.</p>}
      <ol className="flex flex-col gap-3">
        {preconditions.map((precondition, index) => {
          const n = index + 1;
          const at = `${path}/preconditions/${index}`;
          return (
            <li
              key={index}
              className="flex flex-col gap-2 rounded-md border border-line bg-surface p-3"
            >
              <CelField
                label={`Precondition ${n} expression`}
                hint="CEL, true when the Operation may run."
                value={asText(precondition['expression'])}
                error={errorsAt(issues, `${at}/expression`, at)}
                placeholder='record(params.contract).status == "draft"'
                onChange={(text) => {
                  update(index, 'expression', text);
                }}
              />
              <div className="flex flex-wrap items-end gap-2">
                <Input
                  label={`Precondition ${n} message`}
                  className="min-w-64 flex-1"
                  value={asText(precondition['message'])}
                  onChange={(event) => {
                    update(index, 'message', event.target.value);
                  }}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove precondition ${n}`}
                  onClick={() => {
                    change((target) => {
                      target['preconditions'] = asList(target['preconditions']).filter(
                        (_, i) => i !== index,
                      );
                    });
                  }}
                >
                  Remove
                </Button>
              </div>
            </li>
          );
        })}
      </ol>
      <div>
        <Button
          onClick={() => {
            change((target) => {
              target['preconditions'] = [
                ...asList(target['preconditions']),
                { expression: '', message: '' },
              ];
            });
          }}
        >
          Add precondition
        </Button>
      </div>
    </div>
  );
}

function StepValues({
  step,
  n,
  at,
  attributes,
  issues,
  update,
}: {
  step: Doc;
  n: number;
  at: string;
  attributes: string[];
  issues: SchemaIssue[];
  update: (mutate: (step: Doc) => void) => void;
}) {
  const values = isRecord(step['values']) ? step['values'] : {};
  const unset = attributes.filter((a) => !(a in values));
  const [adding, setAdding] = useState('');
  const choice = unset.includes(adding) ? adding : (unset[0] ?? '');
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-xs font-medium text-muted">Step {n} values</legend>
      {Object.entries(values).map(([attribute, expression]) => (
        <div key={attribute} className="flex items-start gap-2">
          <CelField
            className="flex-1"
            label={`Step ${n} value of ${attribute}`}
            value={asText(expression)}
            error={errorAt(issues, `${at}/values/${attribute}`)}
            onChange={(text) => {
              update((s) => {
                child(s, 'values')[attribute] = text;
              });
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            className="mt-5"
            aria-label={`Remove value of ${attribute} from step ${n}`}
            onClick={() => {
              update((s) => {
                const next = child(s, 'values');
                delete next[attribute];
              });
            }}
          >
            Remove
          </Button>
        </div>
      ))}
      {unset.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          <Select
            label={`Step ${n} Attribute to set`}
            value={choice}
            options={unset.map((a) => ({ value: a, label: a }))}
            onChange={(event) => {
              setAdding(event.target.value);
            }}
          />
          <Button
            onClick={() => {
              update((s) => {
                child(s, 'values')[choice] = '';
              });
            }}
          >
            Set Attribute
          </Button>
        </div>
      )}
    </fieldset>
  );
}

function Steps({
  operation,
  path,
  issues,
  document,
  change,
}: {
  operation: Doc;
  path: string;
  issues: SchemaIssue[];
  document: SchemaDocument;
  change: Change;
}) {
  const steps = asList(operation['steps']).filter(isRecord);
  const kinds = Object.keys(document.kinds ?? {});
  const relations = Object.keys(document.relations ?? {});
  const list = useRef<HTMLOListElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const refocus = useRef<{ index: number; direction: 'up' | 'down' } | null>(null);
  const move = (from: number, to: number) => {
    if (to < 0 || to >= steps.length || from === to) return;
    change((target) => {
      moveStep(target, from, to);
    });
    setAnnouncement(`Step ${from + 1} moved to position ${to + 1} of ${steps.length}.`);
  };
  // After a move by button, keep the focus on the same button of the step that moved.
  useEffect(() => {
    const pending = refocus.current;
    if (pending === null) return;
    refocus.current = null;
    const other = pending.direction === 'up' ? 'down' : 'up';
    const button = list.current?.querySelector<HTMLButtonElement>(
      `[data-move="${pending.direction}-${pending.index}"]`,
    );
    const fallback = list.current?.querySelector<HTMLButtonElement>(
      `[data-move="${other}-${pending.index}"]`,
    );
    (button?.disabled === false ? button : fallback)?.focus();
  });
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        The effects of the Operation, in order. <code>$step[n]</code> is the Record step n wrote (n
        counts from 0); moving a step renumbers the references to it.
      </p>
      <p role="status" aria-label="Step order" className="sr-only">
        {announcement}
      </p>
      {errorAt(issues, `${path}/steps`) !== undefined && (
        <p className="text-xs text-danger">{errorAt(issues, `${path}/steps`)}</p>
      )}
      <ol ref={list} className="flex flex-col gap-3">
        {steps.map((step, index) => {
          const n = index + 1;
          const at = `${path}/steps/${index}`;
          const action = OPERATION_ACTIONS.find((a) => a === step['step']);
          const fields = action === undefined ? [] : STEP_FIELDS[action];
          const update = (mutate: (s: Doc) => void) => {
            change((target) => {
              const found = asList(target['steps'])[index];
              if (isRecord(found)) mutate(found);
            });
          };
          const kind = asText(step['kind']);
          const relation = asText(step['relation']);
          const attributes = fields.includes('kind')
            ? Object.keys(document.kinds?.[kind]?.attributes ?? {})
            : Object.keys(document.relations?.[relation]?.attributes ?? {});
          return (
            <li
              key={index}
              className={`flex flex-col gap-3 rounded-md border bg-surface p-3 ${
                over === index && dragging !== null && dragging !== index
                  ? 'border-accent'
                  : 'border-line'
              } ${dragging === index ? 'opacity-50' : ''}`}
              aria-label={`Step ${n}`}
              onDragOver={(event) => {
                if (dragging === null) return;
                event.preventDefault();
                setOver(index);
              }}
              onDrop={(event) => {
                if (dragging === null) return;
                event.preventDefault();
                move(dragging, index);
                setDragging(null);
                setOver(null);
              }}
            >
              <div className="flex flex-wrap items-end gap-2">
                <span
                  draggable
                  aria-hidden="true"
                  title="Drag to reorder"
                  className="mb-1 cursor-grab px-1 text-muted select-none"
                  onDragStart={(event) => {
                    event.dataTransfer.effectAllowed = 'move';
                    event.dataTransfer.setData('text/plain', String(index));
                    setDragging(index);
                  }}
                  onDragEnd={() => {
                    setDragging(null);
                    setOver(null);
                  }}
                >
                  &#x2807;&#x2807;
                </span>
                <Select
                  label={`Step ${n} action`}
                  value={action ?? asText(step['step'])}
                  error={errorAt(issues, at)}
                  options={[
                    ...(action === undefined
                      ? [{ value: asText(step['step']), label: asText(step['step']) }]
                      : []),
                    ...OPERATION_ACTIONS.map((a) => ({ value: a, label: a })),
                  ]}
                  onChange={(event) => {
                    const next = event.target.value as OperationAction;
                    change((target, draft) => {
                      const found = asList(target['steps'])[index];
                      if (isRecord(found)) changeStepAction(found, next, draft);
                    });
                  }}
                />
                {fields.includes('kind') && (
                  <Select
                    label={`Step ${n} Kind`}
                    value={kind}
                    error={errorAt(issues, `${at}/kind`)}
                    options={(kinds.includes(kind) ? kinds : [kind, ...kinds]).map((k) => ({
                      value: k,
                      label: k,
                    }))}
                    onChange={(event) => {
                      const value = event.target.value;
                      update((s) => {
                        s['kind'] = value;
                      });
                    }}
                  />
                )}
                {fields.includes('relation') && (
                  <Select
                    label={`Step ${n} Relation`}
                    value={relation}
                    error={errorAt(issues, `${at}/relation`)}
                    options={(relations.includes(relation)
                      ? relations
                      : [relation, ...relations]
                    ).map((r) => ({ value: r, label: r }))}
                    onChange={(event) => {
                      const value = event.target.value;
                      update((s) => {
                        s['relation'] = value;
                      });
                    }}
                  />
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  data-move={`up-${index}`}
                  aria-label={`Move step ${n} up`}
                  disabled={index === 0}
                  onClick={() => {
                    move(index, index - 1);
                    refocus.current = { index: index - 1, direction: 'up' };
                  }}
                >
                  Up
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  data-move={`down-${index}`}
                  aria-label={`Move step ${n} down`}
                  disabled={index === steps.length - 1}
                  onClick={() => {
                    move(index, index + 1);
                    refocus.current = { index: index + 1, direction: 'down' };
                  }}
                >
                  Down
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove step ${n}`}
                  onClick={() => {
                    change((target) => {
                      target['steps'] = asList(target['steps']).filter((_, i) => i !== index);
                    });
                  }}
                >
                  Remove
                </Button>
              </div>
              {(['target', 'from', 'to'] as const)
                .filter((key) => fields.includes(key))
                .map((key) => (
                  <CelField
                    key={key}
                    label={`Step ${n} ${key}`}
                    hint="CEL, a Record reference such as params.supplier or $step[0]."
                    value={asText(step[key])}
                    error={errorAt(issues, `${at}/${key}`)}
                    onChange={(text) => {
                      update((s) => {
                        s[key] = text;
                      });
                    }}
                  />
                ))}
              {fields.includes('values') && (
                <StepValues
                  step={step}
                  n={n}
                  at={at}
                  attributes={attributes}
                  issues={issues}
                  update={update}
                />
              )}
            </li>
          );
        })}
      </ol>
      <div>
        <Button
          onClick={() => {
            change((target, draft) => {
              const step: Doc = {};
              changeStepAction(step, 'create_record', draft);
              step['values'] = {};
              target['steps'] = [...asList(target['steps']), step];
            });
          }}
        >
          Add step
        </Button>
      </div>
    </div>
  );
}

function Governance({
  operation,
  path,
  issues,
  change,
}: {
  operation: Doc;
  path: string;
  issues: SchemaIssue[];
  change: Change;
}) {
  const policy = reviewPolicyOf(operation);
  const limit = isRecord(operation['rate_limit']) ? operation['rate_limit'] : {};
  const setLimit = (key: 'max_calls' | 'per_seconds', text: string) => {
    change((target) => {
      const next = isRecord(target['rate_limit']) ? target['rate_limit'] : {};
      setOrDelete(next, key, wholeNumber(text));
      if (Object.keys(next).length === 0) delete target['rate_limit'];
      else target['rate_limit'] = next;
    });
  };
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Select
        label="Review policy"
        value={policy.mode}
        error={policy.mode === 'expression' ? undefined : errorAt(issues, `${path}/review_policy`)}
        options={[
          { value: 'never', label: 'never: commit directly' },
          { value: 'always', label: 'always: open a Proposal' },
          { value: 'expression', label: 'expression: Proposal when true' },
        ]}
        onChange={(event) => {
          const mode = event.target.value as ReviewMode;
          change((target) => {
            setReviewPolicy(target, mode, policy.expression);
          });
        }}
      />
      {policy.mode === 'expression' && (
        <CelField
          label="Review expression"
          hint="CEL, true sends the edits to a Proposal."
          value={policy.expression}
          error={errorsAt(issues, `${path}/review_policy/expression`, `${path}/review_policy`)}
          onChange={(text) => {
            change((target) => {
              setReviewPolicy(target, 'expression', text);
            });
          }}
        />
      )}
      <ListInput
        label="Permissions"
        value={asList(operation['permissions']).map(String)}
        error={errorAt(issues, `${path}/permissions`)}
        onChange={(values) => {
          change((target) => {
            setOrDelete(target, 'permissions', values);
          });
        }}
      />
      <ListInput
        label="Side effects"
        value={asList(operation['side_effects']).map(String)}
        error={errorAt(issues, `${path}/side_effects`)}
        onChange={(values) => {
          change((target) => {
            setOrDelete(target, 'side_effects', values);
          });
        }}
      />
      <Input
        label="Rate limit: calls"
        type="number"
        min={1}
        value={typeof limit['max_calls'] === 'number' ? limit['max_calls'] : ''}
        error={errorAt(issues, `${path}/rate_limit`)}
        onChange={(event) => {
          setLimit('max_calls', event.target.value);
        }}
      />
      <Input
        label="Rate limit: per seconds"
        type="number"
        min={1}
        value={typeof limit['per_seconds'] === 'number' ? limit['per_seconds'] : ''}
        onChange={(event) => {
          setLimit('per_seconds', event.target.value);
        }}
      />
    </div>
  );
}

/** Operation editor: parameters, preconditions, steps, review policy and limits, dry run. */
export function OperationsForm({ document, issues, edit, ids, selected, onSelect, saved }: Props) {
  const operations = document.operations ?? {};
  const names = Object.keys(operations);
  const name = selected !== undefined && selected in operations ? selected : names[0];
  const operation = name === undefined ? undefined : operations[name];

  return (
    <div className="flex flex-col gap-4">
      <ElementPicker
        what="Operation"
        names={names}
        selected={name}
        onSelect={onSelect}
        onAdd={(added) => {
          edit((draft) => {
            addOperation(draft, added, ids);
          });
        }}
        onRemove={(removed) => {
          edit((draft) => {
            draft.operations = removeKey(draft.operations, removed);
          });
        }}
      />
      {name === undefined || operation === undefined ? (
        <p className="text-sm text-muted">No custom Operations.</p>
      ) : (
        <OperationEditor
          key={asText(operation['id']) || name}
          name={name}
          operation={operation}
          document={document}
          issues={issues}
          edit={edit}
          onSelect={onSelect}
          canDryRun={saved?.(name) ?? false}
        />
      )}
    </div>
  );
}

function OperationEditor({
  name,
  operation,
  document,
  issues,
  edit,
  onSelect,
  canDryRun,
}: {
  name: string;
  operation: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  onSelect: (name: string | undefined) => void;
  canDryRun: boolean;
}) {
  const path = `operations/${name}`;
  const change: Change = (mutate) => {
    edit((draft) => {
      const target = draft.operations?.[name];
      if (target !== undefined) mutate(target, draft);
    });
  };
  const kindNames = Object.keys(document.kinds ?? {});
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <ElementName
          what="Operation"
          name={name}
          names={Object.keys(document.operations ?? {})}
          issues={issues}
          path={path}
          onRename={(from, to) => {
            edit((draft) => {
              renameOperation(draft, from, to);
            });
          }}
          onRenamed={onSelect}
        />
        <StatusAndDescription
          what="Operation"
          element={operation}
          onChange={(key, value) => {
            change((target) => {
              setOrDelete(target, key, value);
            });
          }}
        />
      </div>
      <Section title="Parameters">
        <Parameters
          operation={operation}
          path={path}
          issues={issues}
          kindNames={kindNames}
          change={change}
        />
      </Section>
      <Section title="Preconditions">
        <Preconditions operation={operation} path={path} issues={issues} change={change} />
      </Section>
      <Section title="Steps">
        <Steps
          operation={operation}
          path={path}
          issues={issues}
          document={document}
          change={change}
        />
      </Section>
      <Section title="Review and limits">
        <Governance operation={operation} path={path} issues={issues} change={change} />
      </Section>
      <Section title="Dry run">
        {canDryRun ? (
          <OperationDryRun name={name} parameters={asList(operation['parameters'])} />
        ) : (
          <p className="text-sm text-muted">
            Save the change set first: a dry run runs the Operation as saved on the Branch.
          </p>
        )}
      </Section>
    </div>
  );
}
