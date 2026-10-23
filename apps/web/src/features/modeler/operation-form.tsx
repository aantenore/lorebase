import { useEffect, useRef, useState } from 'react';
import {
  OPERATION_ACTIONS,
  STEP_FIELDS,
  type OperationAction,
  type SchemaIssue,
} from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { modelerEditors as modelerEditorsMessages } from '@/messages/modeler-editors';
import { FormAction, FormRow, Section } from '@/ui/layout';
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

const t = modelerEditorsMessages.operation;
const actions = modelerEditorsMessages.actions;

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
      {parameters.length === 0 && <p className="text-sm text-muted">{t.parameters.none}</p>}
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
                label={t.parameters.name(n)}
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
                label={t.parameters.type(n)}
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
                  label={t.parameters.enumValues(n)}
                  value={enumValuesOf(type)}
                  onChange={(values) => {
                    update(index, (p) => {
                      p['type'] = typeFromLabel(label, values);
                    });
                  }}
                />
              ) : (
                <Input
                  label={t.parameters.description(n)}
                  value={asText(parameter['description'])}
                  onChange={(event) => {
                    const value = event.target.value;
                    update(index, (p) => {
                      setOrDelete(p, 'description', value);
                    });
                  }}
                />
              )}
              <FormAction>
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
                  {t.parameters.required(n)}
                </label>
                <Button
                  variant="ghost"
                  aria-label={t.parameters.remove(n)}
                  onClick={() => {
                    change((target) => {
                      target['parameters'] = asList(target['parameters']).filter(
                        (_, i) => i !== index,
                      );
                    });
                  }}
                >
                  {actions.remove}
                </Button>
              </FormAction>
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
          {t.parameters.add}
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
      {preconditions.length === 0 && <p className="text-sm text-muted">{t.preconditions.none}</p>}
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
                label={t.preconditions.expression(n)}
                hint={t.preconditions.hint}
                value={asText(precondition['expression'])}
                error={errorsAt(issues, `${at}/expression`, at)}
                placeholder={t.preconditions.placeholder}
                onChange={(text) => {
                  update(index, 'expression', text);
                }}
              />
              <FormRow>
                <Input
                  label={t.preconditions.message(n)}
                  className="min-w-64 flex-1"
                  value={asText(precondition['message'])}
                  onChange={(event) => {
                    update(index, 'message', event.target.value);
                  }}
                />
                <FormAction>
                  <Button
                    variant="ghost"
                    aria-label={t.preconditions.remove(n)}
                    onClick={() => {
                      change((target) => {
                        target['preconditions'] = asList(target['preconditions']).filter(
                          (_, i) => i !== index,
                        );
                      });
                    }}
                  >
                    {actions.remove}
                  </Button>
                </FormAction>
              </FormRow>
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
          {t.preconditions.add}
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
      <legend className="mb-1 text-xs font-medium text-muted">{t.steps.values(n)}</legend>
      {Object.entries(values).map(([attribute, expression]) => (
        <FormRow key={attribute}>
          <CelField
            className="min-w-0 flex-1"
            label={t.steps.valueOf(n, attribute)}
            value={asText(expression)}
            error={errorAt(issues, `${at}/values/${attribute}`)}
            onChange={(text) => {
              update((s) => {
                child(s, 'values')[attribute] = text;
              });
            }}
          />
          <FormAction>
            <Button
              variant="ghost"
              aria-label={t.steps.removeValue(n, attribute)}
              onClick={() => {
                update((s) => {
                  const next = child(s, 'values');
                  delete next[attribute];
                });
              }}
            >
              {actions.remove}
            </Button>
          </FormAction>
        </FormRow>
      ))}
      {unset.length > 0 && (
        <FormRow>
          <Select
            label={t.steps.attributeToSet(n)}
            value={choice}
            options={unset.map((a) => ({ value: a, label: a }))}
            onChange={(event) => {
              setAdding(event.target.value);
            }}
          />
          <FormAction>
            <Button
              onClick={() => {
                update((s) => {
                  child(s, 'values')[choice] = '';
                });
              }}
            >
              {t.steps.setAttribute}
            </Button>
          </FormAction>
        </FormRow>
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
    setAnnouncement(t.steps.moved(from + 1, to + 1, steps.length));
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
      <p className="text-xs text-muted">{t.steps.intro}</p>
      <p role="status" aria-label={t.steps.orderLabel} className="sr-only">
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
              aria-label={t.steps.item(n)}
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
              <FormRow>
                <FormAction>
                  <span
                    draggable
                    aria-hidden="true"
                    title={t.steps.drag}
                    className="cursor-grab px-1 text-muted select-none"
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
                </FormAction>
                <Select
                  label={t.steps.action(n)}
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
                    label={t.steps.kind(n)}
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
                    label={t.steps.relation(n)}
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
                <FormAction>
                  <Button
                    variant="ghost"
                    data-move={`up-${index}`}
                    aria-label={t.steps.moveUp(n)}
                    disabled={index === 0}
                    onClick={() => {
                      move(index, index - 1);
                      refocus.current = { index: index - 1, direction: 'up' };
                    }}
                  >
                    {actions.up}
                  </Button>
                  <Button
                    variant="ghost"
                    data-move={`down-${index}`}
                    aria-label={t.steps.moveDown(n)}
                    disabled={index === steps.length - 1}
                    onClick={() => {
                      move(index, index + 1);
                      refocus.current = { index: index + 1, direction: 'down' };
                    }}
                  >
                    {actions.down}
                  </Button>
                  <Button
                    variant="ghost"
                    aria-label={t.steps.remove(n)}
                    onClick={() => {
                      change((target) => {
                        target['steps'] = asList(target['steps']).filter((_, i) => i !== index);
                      });
                    }}
                  >
                    {actions.remove}
                  </Button>
                </FormAction>
              </FormRow>
              {(['target', 'from', 'to'] as const)
                .filter((key) => fields.includes(key))
                .map((key) => (
                  <CelField
                    key={key}
                    label={t.steps.reference(n, key)}
                    hint={t.steps.referenceHint}
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
          {t.steps.add}
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
        label={t.governance.reviewPolicy}
        value={policy.mode}
        error={policy.mode === 'expression' ? undefined : errorAt(issues, `${path}/review_policy`)}
        options={[
          { value: 'never', label: t.governance.never },
          { value: 'always', label: t.governance.always },
          { value: 'expression', label: t.governance.expression },
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
          label={t.governance.reviewExpression}
          hint={t.governance.reviewExpressionHint}
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
        label={t.governance.permissions}
        value={asList(operation['permissions']).map(String)}
        error={errorAt(issues, `${path}/permissions`)}
        onChange={(values) => {
          change((target) => {
            setOrDelete(target, 'permissions', values);
          });
        }}
      />
      <ListInput
        label={t.governance.sideEffects}
        value={asList(operation['side_effects']).map(String)}
        error={errorAt(issues, `${path}/side_effects`)}
        onChange={(values) => {
          change((target) => {
            setOrDelete(target, 'side_effects', values);
          });
        }}
      />
      <Input
        label={t.governance.rateCalls}
        type="number"
        min={1}
        value={typeof limit['max_calls'] === 'number' ? limit['max_calls'] : ''}
        error={errorAt(issues, `${path}/rate_limit`)}
        onChange={(event) => {
          setLimit('max_calls', event.target.value);
        }}
      />
      <Input
        label={t.governance.rateSeconds}
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
        what={t.kind}
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
        <p className="text-sm text-muted">{t.none}</p>
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
          what={t.kind}
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
          what={t.kind}
          element={operation}
          onChange={(key, value) => {
            change((target) => {
              setOrDelete(target, key, value);
            });
          }}
        />
      </div>
      <Section title={t.parameters.title}>
        <Parameters
          operation={operation}
          path={path}
          issues={issues}
          kindNames={kindNames}
          change={change}
        />
      </Section>
      <Section title={t.preconditions.title}>
        <Preconditions operation={operation} path={path} issues={issues} change={change} />
      </Section>
      <Section title={t.steps.title}>
        <Steps
          operation={operation}
          path={path}
          issues={issues}
          document={document}
          change={change}
        />
      </Section>
      <Section title={t.governance.title}>
        <Governance operation={operation} path={path} issues={issues} change={change} />
      </Section>
      <Section title={t.dryRun.title}>
        {canDryRun ? (
          <OperationDryRun name={name} parameters={asList(operation['parameters'])} />
        ) : (
          <p className="text-sm text-muted">{t.dryRun.saveFirst}</p>
        )}
      </Section>
    </div>
  );
}
