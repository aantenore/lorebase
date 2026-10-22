import { Link } from '@tanstack/react-router';
import { Fragment, useState } from 'react';
import { describeError, ValidationError } from '@/api';
import type {
  DecideSuggestionsResult,
  SchemaSuggestion,
  SuggestionExample,
  SuggestionStatus,
  SuggestionType,
} from '@/api/suggestion-types';
import { useDecideSuggestions, useSchemaSuggestions } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { percent } from '@/features/extraction/item-state';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Badge, EmptyState, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import {
  DEFAULT_SUGGESTION_FILTER,
  isDecidable,
  ownerLabel,
  STATUS_TONE,
  toDecideInput,
  toSuggestionFilter,
  TYPE_LABEL,
  type SuggestionFilterState,
} from './suggestion-state';

function Filters({
  filter,
  onChange,
}: {
  filter: SuggestionFilterState;
  onChange: (filter: SuggestionFilterState) => void;
}) {
  return (
    <form
      aria-label="Filter suggestions"
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <Select
        label="Status"
        value={filter.status}
        options={[
          { value: 'all', label: 'All' },
          { value: 'pending', label: 'Pending' },
          { value: 'accepted', label: 'Accepted' },
          { value: 'rejected', label: 'Rejected' },
        ]}
        onChange={(event) => {
          onChange({ ...filter, status: event.target.value as SuggestionStatus | 'all' });
        }}
      />
      <Select
        label="Type"
        value={filter.type}
        options={[
          { value: 'all', label: 'All' },
          { value: 'kind', label: 'Kind' },
          { value: 'attribute', label: 'Attribute' },
          { value: 'relation', label: 'Relation' },
        ]}
        onChange={(event) => {
          onChange({ ...filter, type: event.target.value as SuggestionType | 'all' });
        }}
      />
      <label className="flex h-9 items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={filter.belowMinSupport}
          onChange={(event) => {
            onChange({ ...filter, belowMinSupport: event.target.checked });
          }}
        />
        Include below min support
      </label>
    </form>
  );
}

function ExampleLink({ example }: { example: SuggestionExample }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  return (
    <li className="flex flex-col gap-0.5">
      <blockquote className="border-l-2 border-line pl-2 text-ink">{example.quote}</blockquote>
      <Link
        to="/$domain/documents/$document"
        params={{ domain: scope.domain, document: example.document_id }}
        search={{ ...scopeSearch, start: example.start, end: example.end }}
        className="text-accent hover:underline"
      >
        View in document
      </Link>
    </li>
  );
}

function Detail({
  suggestion,
  rename,
  onRename,
}: {
  suggestion: SchemaSuggestion;
  rename: string;
  onRename: (name: string) => void;
}) {
  const guess = suggestion.type_guess;
  return (
    <section aria-label={`Details of ${suggestion.name}`} className="flex flex-col gap-3 text-sm">
      {suggestion.withheld && (
        <p className="text-warning">
          Samples and evidence are withheld: this matches a sensitive Attribute.
        </p>
      )}
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted">Labels</dt>
        <dd>{suggestion.labels.length === 0 ? 'none' : suggestion.labels.join(', ')}</dd>
        <dt className="text-muted">Found by</dt>
        <dd>{suggestion.sources.join(', ')}</dd>
        {guess !== null && (
          <>
            <dt className="text-muted">Type guess</dt>
            <dd>
              {guess.type} ({percent(guess.confidence)}): {guess.reasons.join('; ')}
            </dd>
          </>
        )}
        {suggestion.duplicate_of !== null && (
          <>
            <dt className="text-muted">Duplicate of</dt>
            <dd>
              {suggestion.duplicate_of.section}.{suggestion.duplicate_of.api_name}
            </dd>
          </>
        )}
        {suggestion.decided_by !== null && (
          <>
            <dt className="text-muted">Decided by</dt>
            <dd>{suggestion.decided_by}</dd>
          </>
        )}
      </dl>
      {suggestion.samples.length > 0 && (
        <div>
          <h4 className="font-medium">Samples</h4>
          <ul className="list-disc pl-5">
            {suggestion.samples.map((sample) => (
              <li key={sample}>{sample}</li>
            ))}
          </ul>
        </div>
      )}
      {suggestion.examples.length > 0 && (
        <div>
          <h4 className="font-medium">Evidence</h4>
          <ul className="flex flex-col gap-2">
            {suggestion.examples.map((example) => (
              <ExampleLink
                key={`${example.document_id}-${example.start.toString()}`}
                example={example}
              />
            ))}
          </ul>
        </div>
      )}
      {isDecidable(suggestion) && (
        <Input
          label={`Accept ${suggestion.name} as`}
          hint="Optional api name to use instead of the proposed one."
          value={rename}
          placeholder={suggestion.name}
          className="max-w-xs"
          onChange={(event) => {
            onRename(event.target.value);
          }}
        />
      )}
    </section>
  );
}

/** Why Keeper refused the decisions, or the message of any other error. */
function Refusal({ error }: { error: unknown }) {
  const fields = error instanceof ValidationError ? error.fields : [];
  return (
    <section
      role="alert"
      aria-label="Keeper refused the decisions"
      className="flex flex-col gap-2 rounded-md border border-danger p-3 text-sm text-danger"
    >
      <p>{describeError(error)}</p>
      {fields.length > 0 && (
        <ul className="list-disc pl-5">
          {fields.map((field, index) => (
            <li key={`${field.pointer}-${index.toString()}`}>
              {field.pointer}: {field.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Outcome({ domain, result }: { domain: string; result: DecideSuggestionsResult }) {
  const scopeSearch = useScopeSearch();
  const { proposal } = result;
  return (
    <section
      aria-label="Last decision"
      className="flex flex-col gap-1 rounded-md border border-success p-3 text-sm"
    >
      <p role="status">{result.suggestions.length.toString()} suggestion(s) decided.</p>
      {proposal !== null && (
        <>
          <p>
            Proposal{' '}
            <Link
              to="/$domain/review/$proposal"
              params={{ domain, proposal: proposal.id }}
              search={scopeSearch}
              className="text-accent hover:underline"
            >
              #{proposal.id} {proposal.title}
            </Link>{' '}
            on branch {proposal.branch}.
          </p>
          <ul className="list-disc pl-5">
            {proposal.changes.map((change) => (
              <li key={change.id}>
                {change.op} {change.section}.{change.api_name}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/**
 * Review: schema suggestions. Kinds, Attributes and Relations found in the documents, with
 * the evidence for each. Accepted ones of one request become one schema change set on a new
 * Branch, proposed as a Proposal.
 */
export function SuggestionsPanel({ domain }: { domain: string }) {
  const [filter, setFilter] = useState<SuggestionFilterState>(DEFAULT_SUGGESTION_FILTER);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [renames, setRenames] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [result, setResult] = useState<DecideSuggestionsResult | null>(null);
  const query = useSchemaSuggestions(domain, toSuggestionFilter(filter));
  const decide = useDecideSuggestions(domain);

  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const chosen = rows.filter((row) => selected.has(row.id));

  const send = (decision: 'accepted' | 'rejected' | 'pending') => {
    decide.mutate(toDecideInput(chosen, decision, renames), {
      onSuccess: (decided) => {
        setResult(decided);
        setSelected(new Set());
        setRenames({});
      },
    });
  };
  const toggle = (id: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  const decidable = rows.filter(isDecidable);
  const allChosen = decidable.length > 0 && decidable.every((row) => selected.has(row.id));

  return (
    <div className="flex flex-col gap-4">
      <Filters
        filter={filter}
        onChange={(next) => {
          setFilter(next);
          setSelected(new Set());
        }}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            send('accepted');
          }}
        >
          Accept selected
        </Button>
        <Button
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            send('rejected');
          }}
        >
          Reject selected
        </Button>
        <Button
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            send('pending');
          }}
        >
          Reset
        </Button>
        <span role="status" className="text-sm text-muted">
          {chosen.length.toString()} selected
        </span>
      </div>
      {decide.isError && <Refusal error={decide.error} />}
      {result !== null && !decide.isError && <Outcome domain={domain} result={result} />}
      <QueryBoundary query={query}>
        {() =>
          rows.length === 0 ? (
            <EmptyState>No schema suggestions match this filter.</EmptyState>
          ) : (
            <Table caption="Schema suggestions">
              <THead>
                <TR>
                  <TH>
                    <input
                      type="checkbox"
                      aria-label="Select all suggestions"
                      checked={allChosen}
                      disabled={decidable.length === 0}
                      onChange={(event) => {
                        setSelected(
                          event.target.checked ? new Set(decidable.map((r) => r.id)) : new Set(),
                        );
                      }}
                    />
                  </TH>
                  <TH>Type</TH>
                  <TH>Name</TH>
                  <TH>Owner or ends</TH>
                  <TH>Support</TH>
                  <TH>Documents</TH>
                  <TH>Confidence</TH>
                  <TH>Type guess</TH>
                  <TH>Status</TH>
                  <TH>Notes</TH>
                </TR>
              </THead>
              <TBody>
                {rows.map((row) => (
                  <Fragment key={row.id}>
                    <TR>
                      <TD>
                        <input
                          type="checkbox"
                          aria-label={`Select ${row.name}`}
                          checked={selected.has(row.id)}
                          disabled={!isDecidable(row)}
                          onChange={(event) => {
                            toggle(row.id, event.target.checked);
                          }}
                        />
                      </TD>
                      <TD>{TYPE_LABEL[row.type]}</TD>
                      <TD>
                        <button
                          type="button"
                          aria-expanded={open === row.id}
                          className="text-accent hover:underline"
                          onClick={() => {
                            setOpen(open === row.id ? null : row.id);
                          }}
                        >
                          {row.name}
                        </button>
                      </TD>
                      <TD>{ownerLabel(row)}</TD>
                      <TD>{row.support}</TD>
                      <TD>{row.documents}</TD>
                      <TD>{percent(row.confidence)}</TD>
                      <TD>
                        {row.type_guess === null
                          ? ''
                          : `${row.type_guess.type} (${percent(row.type_guess.confidence)})`}
                      </TD>
                      <TD>
                        <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>
                      </TD>
                      <TD>
                        <span className="flex flex-wrap gap-1">
                          {row.duplicate_of !== null && <Badge tone="danger">duplicate</Badge>}
                          {row.withheld && <Badge tone="warning">withheld</Badge>}
                          {row.proposal_id !== null && (
                            <Badge tone="accent">#{row.proposal_id}</Badge>
                          )}
                        </span>
                      </TD>
                    </TR>
                    {open === row.id && (
                      <TR>
                        <TD colSpan={10}>
                          <Detail
                            suggestion={row}
                            rename={renames[row.id] ?? ''}
                            onRename={(name) => {
                              setRenames((current) => ({ ...current, [row.id]: name }));
                            }}
                          />
                        </TD>
                      </TR>
                    )}
                  </Fragment>
                ))}
              </TBody>
            </Table>
          )
        }
      </QueryBoundary>
      {query.hasNextPage && (
        <div>
          <Button
            disabled={query.isFetchingNextPage}
            onClick={() => {
              void query.fetchNextPage();
            }}
          >
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
