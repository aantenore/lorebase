import {
  credentialHeader,
  isSecretReference,
  PAGINATION_TYPES,
  type SchemaIssue,
} from '@/api/schema-rules';
import { SOURCE_TYPES, type SourceType } from '@/api/types';
import { Button } from '@/ui/button';
import { Input, Select, Textarea } from '@/ui/field';
import { Section } from '@/ui/layout';
import type { SchemaDocument } from './document';
import { ElementName, ElementPicker, StatusAndDescription } from './element-controls';
import {
  addSource,
  appliesTo,
  changeSourceType,
  child,
  removeSource,
  renameSource,
  setOrDelete,
  wholeNumber,
} from './element-edits';
import {
  asList,
  asText,
  errorAt,
  errorsAt,
  isRecord,
  renameKey,
  type Edit,
  type ElementIds,
} from './form-helpers';
import { KeyInput } from './key-input';
import { ListInput } from './list-input';
import { SourcePreview } from './source-preview-panel';
import { TransformsEditor } from './transform-editor';
import { transformsOf } from './transform-edits';

type Doc = Record<string, unknown>;
type Change = (mutate: (source: Doc, draft: SchemaDocument) => void) => void;

interface Props {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

const SECRET_HINT = 'A secret reference such as secret://env/ERP_DSN; never a literal.';
const MERGE_POLICIES = ['proposal', 'auto_when_clean', 'auto_with_authority'];

const SECRET_MESSAGE = 'Must be a secret:// reference, never a literal.';

/** The client-side check of a secret field; Keeper checks again when the change set is sent. */
const secretProblem = (value: string): string | undefined =>
  value === '' || isSecretReference(value) ? undefined : SECRET_MESSAGE;

function TextField({
  label,
  source,
  field,
  path,
  issues,
  change,
  hint,
  placeholder,
  className,
}: {
  label: string;
  source: Doc;
  field: string;
  path: string;
  issues: SchemaIssue[];
  change: Change;
  hint?: string;
  placeholder?: string;
  className?: string;
}) {
  return (
    <Input
      label={label}
      hint={hint}
      placeholder={placeholder}
      className={className}
      value={asText(source[field])}
      error={errorAt(issues, `${path}/${field}`)}
      onChange={(event) => {
        const value = event.target.value;
        change((target) => {
          setOrDelete(target, field, value);
        });
      }}
    />
  );
}

function Connection({
  source,
  path,
  issues,
  change,
}: {
  source: Doc;
  path: string;
  issues: SchemaIssue[];
  change: Change;
}) {
  const type = asText(source['type']) || 'csv';
  const connection = asText(source['connection']);
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Select
        label="Source type"
        value={type}
        error={errorAt(issues, `${path}/type`)}
        options={[
          ...(SOURCE_TYPES.some((t) => t === type) ? [] : [{ value: type, label: type }]),
          ...SOURCE_TYPES.map((t) => ({ value: t, label: t })),
        ]}
        onChange={(event) => {
          const next = event.target.value as SourceType;
          change((target) => {
            changeSourceType(target, next);
          });
        }}
      />
      <Input
        label="Connection"
        hint={type === 'http_json' ? `The URL, as ${SECRET_HINT}` : SECRET_HINT}
        value={connection}
        placeholder="secret://env/NAME"
        error={errorAt(issues, `${path}/connection`) ?? secretProblem(connection)}
        onChange={(event) => {
          const value = event.target.value;
          change((target) => {
            target['connection'] = value;
          });
        }}
      />
      {appliesTo('table', type) && (
        <TextField
          label="Table"
          hint="Optionally schema-qualified; empty reads the table named like the Source."
          source={source}
          field="table"
          path={path}
          issues={issues}
          change={change}
        />
      )}
      {appliesTo('query', type) && (
        <Textarea
          label="Query"
          hint="A read-only SELECT."
          className="sm:col-span-2"
          value={asText(source['query'])}
          error={errorAt(issues, `${path}/query`)}
          onChange={(event) => {
            const value = event.target.value;
            change((target) => {
              setOrDelete(target, 'query', value);
            });
          }}
        />
      )}
      {appliesTo('records_path', type) && (
        <TextField
          label="Records path"
          hint="Dotted path of the array of records, such as data.items; empty: the document is the array."
          source={source}
          field="records_path"
          path={path}
          issues={issues}
          change={change}
        />
      )}
      {appliesTo('flatten_separator', type) && (
        <TextField
          label="Flatten separator"
          hint="Nested objects become parent<separator>child columns; empty keeps them as JSON."
          source={source}
          field="flatten_separator"
          path={path}
          issues={issues}
          change={change}
        />
      )}
    </div>
  );
}

function Headers({
  http,
  at,
  issues,
  changeHttp,
}: {
  http: Doc;
  at: string;
  issues: SchemaIssue[];
  changeHttp: (mutate: (http: Doc) => void) => void;
}) {
  const headers = isRecord(http['headers']) ? http['headers'] : {};
  const entries = Object.entries(headers);
  return (
    <fieldset className="flex flex-col gap-2 sm:col-span-2">
      <legend className="mb-1 text-xs font-medium text-muted">Request headers</legend>
      <p className="text-xs text-muted">
        Headers that carry credentials (auth, token, key, cookie...) take a secret:// reference.
      </p>
      {entries.map(([header, value], index) => {
        const n = index + 1;
        const text = asText(value);
        const local = credentialHeader(header) ? secretProblem(text || 'x') : undefined;
        return (
          <div key={index} className="flex flex-wrap items-start gap-2">
            <KeyInput
              label={`Header ${n} name`}
              name={header}
              taken={(candidate) => candidate !== header && candidate in headers}
              onRename={(next) => {
                changeHttp((h) => {
                  h['headers'] = renameKey(child(h, 'headers'), header, next);
                });
              }}
            />
            <Input
              label={`Header ${n} value`}
              className="min-w-64 flex-1"
              value={text}
              error={errorAt(issues, `${at}/headers/${header}`) ?? local}
              onChange={(event) => {
                const next = event.target.value;
                changeHttp((h) => {
                  child(h, 'headers')[header] = next;
                });
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              className="mt-5"
              aria-label={`Remove header ${n}`}
              onClick={() => {
                changeHttp((h) => {
                  const next = child(h, 'headers');
                  delete next[header];
                  if (Object.keys(next).length === 0) delete h['headers'];
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
          onClick={() => {
            changeHttp((h) => {
              const next = child(h, 'headers');
              let n = entries.length + 1;
              while (`X-Header-${n}` in next) n += 1;
              next[`X-Header-${n}`] = '';
            });
          }}
        >
          Add header
        </Button>
      </div>
    </fieldset>
  );
}

function Http({
  source,
  path,
  issues,
  change,
}: {
  source: Doc;
  path: string;
  issues: SchemaIssue[];
  change: Change;
}) {
  const at = `${path}/http`;
  const http = isRecord(source['http']) ? source['http'] : {};
  const pagination = isRecord(http['pagination']) ? http['pagination'] : {};
  const type = asText(pagination['type']) || 'none';
  const changeHttp = (mutate: (http: Doc) => void) => {
    change((target) => {
      const next = child(target, 'http');
      mutate(next);
      if (Object.keys(next).length === 0) delete target['http'];
    });
  };
  const setPagination = (key: string, value: unknown) => {
    changeHttp((h) => {
      const next = child(h, 'pagination');
      setOrDelete(next, key, value);
      if (Object.keys(next).length === 0) delete h['pagination'];
    });
  };
  const text = (key: string, label: string, placeholder?: string) => (
    <Input
      key={key}
      label={label}
      placeholder={placeholder}
      value={asText(pagination[key])}
      error={errorAt(issues, `${at}/pagination/${key}`)}
      onChange={(event) => {
        setPagination(key, event.target.value);
      }}
    />
  );
  const number = (
    key: string,
    label: string,
    scope: Doc,
    set: (value: unknown) => void,
    err: string,
  ) => {
    const value = scope[key];
    return (
      <Input
        key={key}
        label={label}
        type="number"
        min={key === 'start_page' ? 0 : 1}
        value={typeof value === 'number' ? value : ''}
        error={errorAt(issues, err)}
        onChange={(event) => {
          set(wholeNumber(event.target.value));
        }}
      />
    );
  };
  const paged = type !== 'none' && type !== 'link';
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Headers http={http} at={at} issues={issues} changeHttp={changeHttp} />
      <Select
        label="Pagination"
        value={type}
        error={errorAt(issues, `${at}/pagination/type`)}
        options={PAGINATION_TYPES.map((t) => ({ value: t, label: t }))}
        onChange={(event) => {
          const value = event.target.value;
          setPagination('type', value === 'none' ? undefined : value);
        }}
      />
      {type === 'cursor' && text('cursor_path', 'Cursor path', 'meta.next_cursor')}
      {type === 'cursor' && text('cursor_param', 'Cursor parameter', 'cursor')}
      {type === 'page' && text('page_param', 'Page parameter', 'page')}
      {type === 'page' &&
        number(
          'start_page',
          'Start page',
          pagination,
          (v) => {
            setPagination('start_page', v);
          },
          `${at}/pagination/start_page`,
        )}
      {type === 'offset' && text('offset_param', 'Offset parameter', 'offset')}
      {paged && text('size_param', 'Page size parameter')}
      {paged &&
        number(
          'page_size',
          'Page size',
          pagination,
          (v) => {
            setPagination('page_size', v);
          },
          `${at}/pagination/page_size`,
        )}
      <Input
        label="Since parameter"
        hint="Query parameter that carries the watermark of the previous run."
        value={asText(http['since_param'])}
        error={errorAt(issues, `${at}/since_param`)}
        onChange={(event) => {
          const value = event.target.value;
          changeHttp((h) => {
            setOrDelete(h, 'since_param', value);
          });
        }}
      />
      {number(
        'max_pages',
        'Max pages per run',
        http,
        (v) => {
          changeHttp((h) => {
            setOrDelete(h, 'max_pages', v);
          });
        },
        `${at}/max_pages`,
      )}
    </div>
  );
}

type MappingMode = 'none' | 'column' | 'expression';

function Mapping({
  source,
  path,
  issues,
  document,
  change,
}: {
  source: Doc;
  path: string;
  issues: SchemaIssue[];
  document: SchemaDocument;
  change: Change;
}) {
  const kinds = Object.keys(document.kinds ?? {});
  const kindName = asText(source['target_kind']);
  const kind = document.kinds?.[kindName];
  const primaryKey = kind?.primary_key ?? [];
  const keyMapping = asList(source['key_mapping']).map(asText);
  const attributes = Object.entries(kind?.attributes ?? {}).filter(
    ([, a]) => a.derivation === undefined,
  );
  const mapping = isRecord(source['attribute_mapping']) ? source['attribute_mapping'] : {};
  const relations = Object.entries(document.relations ?? {})
    .filter(([, r]) => r.from_kind === kindName)
    .map(([n]) => n);
  const relationMapping = asList(source['relation_mapping']).filter(isRecord);
  const changeMapping = (
    attribute: string,
    mutate: (entry: Doc | undefined) => Doc | undefined,
  ) => {
    change((target) => {
      const all = child(target, 'attribute_mapping');
      const current = all[attribute];
      const next = mutate(isRecord(current) ? current : undefined);
      if (next === undefined) delete all[attribute];
      else all[attribute] = next;
    });
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Select
          label="Target Kind"
          value={kindName}
          error={errorAt(issues, `${path}/target_kind`)}
          options={[
            ...(kinds.includes(kindName) ? [] : [{ value: kindName, label: kindName || '-' }]),
            ...kinds.map((k) => ({ value: k, label: k })),
          ]}
          onChange={(event) => {
            const value = event.target.value;
            change((target) => {
              target['target_kind'] = value;
            });
          }}
        />
        <TextField
          label="Scope filter"
          hint="Optional filter of the rows read, in the language of the Source."
          source={source}
          field="scope_filter"
          path={path}
          issues={issues}
          change={change}
        />
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-muted">Key mapping</legend>
        {primaryKey.length === 0 ? (
          <p className="text-sm text-muted">{kindName} has no primary key.</p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-3">
            {primaryKey.map((attribute, index) => (
              <Input
                key={attribute}
                label={`Key column for ${attribute}`}
                value={keyMapping[index] ?? ''}
                onChange={(event) => {
                  const value = event.target.value;
                  change((target) => {
                    const next = asList(target['key_mapping']).map(asText);
                    while (next.length < primaryKey.length) next.push('');
                    next[index] = value;
                    target['key_mapping'] = next;
                  });
                }}
              />
            ))}
          </div>
        )}
        {errorAt(issues, `${path}/key_mapping`) !== undefined && (
          <p className="text-xs text-danger">{errorAt(issues, `${path}/key_mapping`)}</p>
        )}
      </fieldset>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-muted">Attribute mapping</legend>
        {attributes.map(([attribute]) => {
          const entry = isRecord(mapping[attribute]) ? mapping[attribute] : undefined;
          const mode: MappingMode =
            entry === undefined
              ? 'none'
              : typeof entry['expression'] === 'string'
                ? 'expression'
                : 'column';
          const transforms = transformsOf(entry);
          return (
            <div key={attribute} className="grid gap-2 sm:grid-cols-[1fr_2fr] sm:items-start">
              <Select
                label={`Mapping of ${attribute}`}
                value={mode}
                options={[
                  { value: 'none', label: 'not mapped' },
                  { value: 'column', label: 'column' },
                  { value: 'expression', label: 'expression' },
                ]}
                onChange={(event) => {
                  const next = event.target.value as MappingMode;
                  changeMapping(attribute, (current) => {
                    if (next === 'none') return undefined;
                    const out: Doc = { ...current };
                    const other = next === 'column' ? 'expression' : 'column';
                    const value = asText(out[other]) || asText(out[next]) || attribute;
                    delete out[other];
                    out[next] = value;
                    return out;
                  });
                }}
              />
              {mode !== 'none' && (
                <Input
                  label={
                    mode === 'column' ? `Column for ${attribute}` : `Expression for ${attribute}`
                  }
                  value={asText(entry?.[mode])}
                  error={errorAt(issues, `${path}/attribute_mapping/${attribute}`)}
                  onChange={(event) => {
                    const value = event.target.value;
                    changeMapping(attribute, (current) => ({ ...current, [mode]: value }));
                  }}
                />
              )}
              {mode !== 'none' && (
                <TransformsEditor
                  attribute={attribute}
                  transforms={transforms}
                  change={(mutate) => {
                    changeMapping(attribute, (current) => {
                      const out: Doc = { ...current };
                      const list = transformsOf(out);
                      setOrDelete(out, 'transforms', mutate(list) ?? list);
                      return out;
                    });
                  }}
                />
              )}
            </div>
          );
        })}
      </fieldset>
      {relations.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-xs font-medium text-muted">Relation mapping</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {relations.map((relation) => {
              const index = relationMapping.findIndex((m) => m['relation'] === relation);
              const columns =
                index < 0 ? [] : asList(relationMapping[index]?.['columns']).map(asText);
              return (
                <ListInput
                  key={relation}
                  label={`Columns for ${relation}`}
                  hint="Foreign key columns of the row; empty: not mapped."
                  value={columns}
                  error={
                    index < 0
                      ? undefined
                      : errorsAt(
                          issues,
                          `${path}/relation_mapping/${index}`,
                          `${path}/relation_mapping/${index}/relation`,
                        )
                  }
                  onChange={(next) => {
                    change((target) => {
                      const list = asList(target['relation_mapping']).filter(isRecord);
                      const found = list.find((m) => m['relation'] === relation);
                      if (next.length === 0) {
                        const rest = list.filter((m) => m !== found);
                        setOrDelete(target, 'relation_mapping', rest);
                      } else if (found !== undefined) {
                        found['columns'] = next;
                        target['relation_mapping'] = list;
                      } else {
                        target['relation_mapping'] = [...list, { relation, columns: next }];
                      }
                    });
                  }}
                />
              );
            })}
          </div>
        </fieldset>
      )}
    </div>
  );
}

function Sync({
  source,
  path,
  issues,
  change,
}: {
  source: Doc;
  path: string;
  issues: SchemaIssue[];
  change: Change;
}) {
  const mode = asText(source['sync_mode']) || 'snapshot';
  const policy = isRecord(source['target_branch_policy']) ? source['target_branch_policy'] : {};
  const setPolicy = (key: string, value: string) => {
    change((target) => {
      const next = child(target, 'target_branch_policy');
      setOrDelete(next, key, value);
      if (Object.keys(next).length === 0) delete target['target_branch_policy'];
    });
  };
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Select
        label="Sync mode"
        value={mode}
        error={errorAt(issues, `${path}/sync_mode`)}
        options={[
          { value: 'snapshot', label: 'snapshot: every run reads everything' },
          { value: 'incremental', label: 'incremental: rows past the watermark' },
        ]}
        onChange={(event) => {
          const value = event.target.value;
          change((target) => {
            target['sync_mode'] = value;
          });
        }}
      />
      <TextField
        label="Watermark column"
        hint={
          mode === 'incremental' ? 'Required for incremental runs.' : 'Used by incremental runs.'
        }
        source={source}
        field="watermark_column"
        path={path}
        issues={issues}
        change={change}
      />
      <TextField
        label="Tombstone column"
        hint="Rows where it is set are deleted."
        source={source}
        field="tombstone_column"
        path={path}
        issues={issues}
        change={change}
      />
      <TextField
        label="Schedule"
        hint="Cron expression; empty: manual runs only."
        placeholder="0 2 * * *"
        source={source}
        field="schedule"
        path={path}
        issues={issues}
        change={change}
      />
      <Input
        label="Branch template"
        placeholder="source/{source}"
        value={asText(policy['branch_template'])}
        error={errorAt(issues, `${path}/target_branch_policy/branch_template`)}
        onChange={(event) => {
          setPolicy('branch_template', event.target.value);
        }}
      />
      <Select
        label="Merge policy"
        value={asText(policy['merge_policy']) || 'proposal'}
        error={errorAt(issues, `${path}/target_branch_policy/merge_policy`)}
        options={MERGE_POLICIES.map((p) => ({ value: p, label: p }))}
        onChange={(event) => {
          setPolicy('merge_policy', event.target.value);
        }}
      />
    </div>
  );
}

function SourceEditor({
  name,
  source,
  document,
  issues,
  edit,
  onSelect,
}: {
  name: string;
  source: Doc;
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  onSelect: (name: string | undefined) => void;
}) {
  const path = `sources/${name}`;
  const change: Change = (mutate) => {
    edit((draft) => {
      const target = draft.sources?.[name];
      if (target !== undefined) mutate(target, draft);
    });
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <ElementName
          what="Source"
          name={name}
          names={Object.keys(document.sources ?? {})}
          issues={issues}
          path={path}
          onRename={(from, to) => {
            edit((draft) => {
              renameSource(draft, from, to);
            });
          }}
          onRenamed={onSelect}
        />
        <StatusAndDescription
          what="Source"
          element={source}
          onChange={(key, value) => {
            change((target) => {
              setOrDelete(target, key, value);
            });
          }}
        />
      </div>
      <Section title="Connection">
        <Connection source={source} path={path} issues={issues} change={change} />
      </Section>
      {asText(source['type']) === 'http_json' && (
        <Section title="HTTP requests">
          <Http source={source} path={path} issues={issues} change={change} />
        </Section>
      )}
      <Section title="Mapping">
        <Mapping source={source} path={path} issues={issues} document={document} change={change} />
      </Section>
      <Section title="Sync and merge">
        <Sync source={source} path={path} issues={issues} change={change} />
      </Section>
      <Section title="Preview">
        <SourcePreview name={name} source={source} document={document} />
      </Section>
    </div>
  );
}

/** Source editor: type and connection, HTTP options, mapping, sync mode and merge policy. */
export function SourcesForm({ document, issues, edit, ids, selected, onSelect }: Props) {
  const sources = document.sources ?? {};
  const names = Object.keys(sources);
  const name = selected !== undefined && selected in sources ? selected : names[0];
  const source = name === undefined ? undefined : sources[name];
  return (
    <div className="flex flex-col gap-4">
      <ElementPicker
        what="Source"
        names={names}
        selected={name}
        onSelect={onSelect}
        onAdd={(added) => {
          edit((draft) => {
            addSource(draft, added, ids);
          });
        }}
        onRemove={(removed) => {
          edit((draft) => {
            removeSource(draft, removed);
          });
        }}
      />
      {name === undefined || source === undefined ? (
        <p className="text-sm text-muted">No Sources.</p>
      ) : (
        <SourceEditor
          key={asText(source['id']) || name}
          name={name}
          source={source}
          document={document}
          issues={issues}
          edit={edit}
          onSelect={onSelect}
        />
      )}
    </div>
  );
}
