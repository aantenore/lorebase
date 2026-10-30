import { Link } from '@tanstack/react-router';
import { Fragment, useState } from 'react';
import { describeError, ValidationError } from '@/api';
import type {
  DecideSuggestionsResult,
  SchemaSuggestion,
  DocumentExample,
  MissExample,
  SuggestionExample,
  SuggestionStatus,
  SuggestionType,
} from '@/api/suggestion-types';
import { useDecideSuggestions, useSchemaSuggestions } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { percent } from '@/features/extraction/item-state';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { review as reviewMessages } from '@/messages/review';
import { Badge, EmptyState, FormAction, FormRow, QueryBoundary } from '@/ui/layout';
import { LearnMore } from '@/ui/learn-more';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import {
  DEFAULT_SUGGESTION_FILTER,
  isDecidable,
  documentsLabel,
  ownerLabel,
  STATUS_TONE,
  toDecideInput,
  toSuggestionFilter,
  TYPE_LABEL,
  type SuggestionFilterState,
} from './suggestion-state';

const text = reviewMessages.suggestions;

const STATUS_OPTIONS = (['all', 'pending', 'accepted', 'rejected'] as const).map((value) => ({
  value,
  label: text.statusOptions[value],
}));

const TYPE_OPTIONS = (
  [
    'all',
    'kind',
    'attribute',
    'relation',
    'kind_alias',
    'attribute_alias',
    'value_alias',
    'attribute_description',
  ] as const
).map((value) => ({
  value,
  label: text.typeOptions[value],
}));

function Filters({
  filter,
  onChange,
}: {
  filter: SuggestionFilterState;
  onChange: (filter: SuggestionFilterState) => void;
}) {
  return (
    <form
      aria-label={text.filterLabel}
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <FormRow>
        <Select
          label={text.status}
          value={filter.status}
          options={STATUS_OPTIONS}
          onChange={(event) => {
            onChange({ ...filter, status: event.target.value as SuggestionStatus | 'all' });
          }}
        />
        <Select
          label={text.type}
          value={filter.type}
          options={TYPE_OPTIONS}
          onChange={(event) => {
            onChange({ ...filter, type: event.target.value as SuggestionType | 'all' });
          }}
        />
        <FormAction>
          <label className="flex h-9 items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={filter.belowMinSupport}
              onChange={(event) => {
                onChange({ ...filter, belowMinSupport: event.target.checked });
              }}
            />
            {text.lowSupport}
          </label>
        </FormAction>
      </FormRow>
    </form>
  );
}

function MissEvidence({ example }: { example: MissExample }) {
  const fixed = (value: number | null) => (value === null ? '' : value.toFixed(2));
  const count = (value: number | null) => (value === null ? '?' : value.toString());
  return (
    <li>
      {text.missed(
        example.phrase,
        count(example.misses),
        count(example.callers),
        fixed(example.similarity),
      )}
    </li>
  );
}

function ExampleItem({ example }: { example: SuggestionExample }) {
  if ('phrase' in example) return <MissEvidence example={example} />;
  return <ExampleLink example={example} />;
}

function exampleKey(example: SuggestionExample, index: number): string {
  if ('phrase' in example) return `miss-${index.toString()}`;
  return `${example.document_id}-${example.start.toString()}`;
}

function ExampleLink({ example }: { example: DocumentExample }) {
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
        {text.viewInDocument}
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
    <section aria-label={text.details(suggestion.name)} className="flex flex-col gap-3 text-sm">
      {suggestion.withheld && <p className="text-warning">{text.withheld}</p>}
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted">{text.labels}</dt>
        <dd>{suggestion.labels.length === 0 ? text.none : suggestion.labels.join(', ')}</dd>
        <dt className="text-muted">{text.foundBy}</dt>
        <dd>{suggestion.sources.join(', ')}</dd>
        {guess !== null && (
          <>
            <dt className="text-muted">{text.typeGuess}</dt>
            <dd>
              {text.typeGuessValue(guess.type, percent(guess.confidence), guess.reasons.join('; '))}
            </dd>
          </>
        )}
        {suggestion.duplicate_of !== null && (
          <>
            <dt className="text-muted">{text.duplicateOf}</dt>
            <dd>
              {suggestion.duplicate_of.section}.{suggestion.duplicate_of.api_name}
            </dd>
          </>
        )}
        {suggestion.value != null && (
          <>
            <dt className="text-muted">{text.standsFor}</dt>
            <dd>{suggestion.value}</dd>
          </>
        )}
        {suggestion.description != null && (
          <>
            <dt className="text-muted">{text.proposedDescription}</dt>
            <dd>{suggestion.description}</dd>
          </>
        )}
        {suggestion.decided_by !== null && (
          <>
            <dt className="text-muted">{text.decidedBy}</dt>
            <dd>{suggestion.decided_by}</dd>
          </>
        )}
      </dl>
      {suggestion.samples.length > 0 && (
        <div>
          <h4 className="font-medium">{text.samples}</h4>
          <ul className="list-disc pl-5">
            {suggestion.samples.map((sample) => (
              <li key={sample}>{sample}</li>
            ))}
          </ul>
        </div>
      )}
      {suggestion.examples.length > 0 && (
        <div>
          <h4 className="font-medium">{text.evidence}</h4>
          <ul className="flex flex-col gap-2">
            {suggestion.examples.map((example, index) => (
              <ExampleItem key={exampleKey(example, index)} example={example} />
            ))}
          </ul>
        </div>
      )}
      {isDecidable(suggestion) && (
        <Input
          label={text.acceptAs(suggestion.name)}
          hint={text.acceptAsHint}
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
      aria-label={text.refusedLabel}
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
      aria-label={text.outcomeLabel}
      className="flex flex-col gap-1 rounded-md border border-success p-3 text-sm"
    >
      <p role="status">{text.decided(result.suggestions.length)}</p>
      {proposal !== null && (
        <>
          <p>
            {text.proposalPrefix}{' '}
            <Link
              to="/$domain/review/$proposal"
              params={{ domain, proposal: proposal.id }}
              search={scopeSearch}
              className="text-accent hover:underline"
            >
              #{proposal.id} {proposal.title}
            </Link>{' '}
            {text.onBranch(proposal.branch)}
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
      <p className="text-sm text-muted">
        {text.description} <LearnMore path={text.docs} />
      </p>
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
          {text.accept}
        </Button>
        <Button
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            send('rejected');
          }}
        >
          {text.reject}
        </Button>
        <Button
          disabled={chosen.length === 0 || decide.isPending}
          onClick={() => {
            send('pending');
          }}
        >
          {text.reset}
        </Button>
        <span role="status" className="text-sm text-muted">
          {text.selected(chosen.length)}
        </span>
      </div>
      {decide.isError && <Refusal error={decide.error} />}
      {result !== null && !decide.isError && <Outcome domain={domain} result={result} />}
      <QueryBoundary query={query}>
        {() =>
          rows.length === 0 ? (
            <EmptyState>{text.empty}</EmptyState>
          ) : (
            <Table caption={text.caption}>
              <THead>
                <TR>
                  <TH>
                    <input
                      type="checkbox"
                      aria-label={text.selectAll}
                      checked={allChosen}
                      disabled={decidable.length === 0}
                      onChange={(event) => {
                        setSelected(
                          event.target.checked ? new Set(decidable.map((r) => r.id)) : new Set(),
                        );
                      }}
                    />
                  </TH>
                  <TH>{text.columns.type}</TH>
                  <TH>{text.columns.name}</TH>
                  <TH>{text.columns.owner}</TH>
                  <TH>{text.columns.support}</TH>
                  <TH>{text.columns.documents}</TH>
                  <TH>{text.columns.confidence}</TH>
                  <TH>{text.columns.typeGuess}</TH>
                  <TH>{text.columns.status}</TH>
                  <TH>{text.columns.notes}</TH>
                </TR>
              </THead>
              <TBody>
                {rows.map((row) => (
                  <Fragment key={row.id}>
                    <TR>
                      <TD>
                        <input
                          type="checkbox"
                          aria-label={text.select(row.name)}
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
                      <TD>{documentsLabel(row)}</TD>
                      <TD>{percent(row.confidence)}</TD>
                      <TD>
                        {row.type_guess === null
                          ? ''
                          : text.guessValue(
                              row.type_guess.type,
                              percent(row.type_guess.confidence),
                            )}
                      </TD>
                      <TD>
                        <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>
                      </TD>
                      <TD>
                        <span className="flex flex-wrap gap-1">
                          {row.duplicate_of !== null && (
                            <Badge tone="danger">{text.duplicate}</Badge>
                          )}
                          {row.withheld && <Badge tone="warning">{text.withheldBadge}</Badge>}
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
            {text.loadMore}
          </Button>
        </div>
      )}
    </div>
  );
}
