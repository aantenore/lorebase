import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { useContextPack, useSchema, useSearch } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { ExampleQuestions, useDomainExamples } from '@/features/explore/domain-examples';
import { useGuideAuth } from '@/features/explore/guide-auth';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';
import { Button } from '@/ui/button';
import { Input, Select, Textarea } from '@/ui/field';
import { FormAction, FormRow, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { embed, parseVector } from '@/api/guide-client';
import { LabCanvas } from '@/features/canvas/lab-canvas';
import { involvedRefs } from '@/features/canvas/lab-graph';
import { PackPanel } from './pack-panel';
import { ParamsForm } from './params-form';
import { defaultParams, needsVector, toRequest, type LabParams } from './params';
import { compareResults } from './result-diff';
import { ResultsColumn } from './results-column';

const T = retrievalLabMessages;

/** A set that wants a vector searches by words while there is none. */
const effective = (params: LabParams, vector: number[] | null): LabParams =>
  needsVector(params.mode) && vector === null ? { ...params, mode: 'lexical' } : params;

/**
 * Retrieval lab: ask a question, compare two parameter sets of the Keeper's search side by side,
 * and see the context pack an agent would receive for it, with the Records involved on a Canvas.
 * The query vector for semantic and hybrid search comes from the optional assistant
 * (`guideUrl`, see `guide-client.ts`) or is pasted by hand; the app itself never embeds text.
 */
export function RetrievalLabPage({
  question,
  budget,
  onAsk,
}: {
  question: string | undefined;
  budget: number | undefined;
  onAsk: (question: string, budget: number | undefined) => void;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const examples = useDomainExamples();
  const { guideUrl, searchLimit } = useConfig();
  const guideAuth = useGuideAuth();
  const schema = useSchema(scope);

  const [text, setText] = useState(question ?? '');
  const [budgetText, setBudgetText] = useState(budget === undefined ? '' : budget.toString());
  const [setA, setSetA] = useState<LabParams>(defaultParams);
  const [setB, setSetB] = useState<LabParams>(() => ({ ...defaultParams(), mode: 'hybrid' }));
  const [pasted, setPasted] = useState('');
  const [lens, setLens] = useState('');
  const lenses = schema.data?.lenses ?? [];
  const withLens = lens === '' ? {} : { lens };

  const embedding = useQuery({
    queryKey: ['guide', 'embed', guideUrl, question],
    queryFn: ({ signal }) => embed(guideUrl ?? '', question ?? '', guideAuth(), signal),
    enabled: guideUrl !== null && question !== undefined,
    retry: false,
    staleTime: Infinity,
  });
  const manual = parseVector(pasted);
  const vector = manual.vector ?? embedding.data?.vector ?? null;
  const waiting = embedding.isFetching && vector === null;

  const requestA = toRequest(effective(setA, vector), question ?? '', vector, searchLimit);
  const requestB = toRequest(effective(setB, vector), question ?? '', vector, searchLimit);
  const ready = (params: LabParams) =>
    question !== undefined && !(needsVector(params.mode) && waiting);
  const searchA = useSearch(scope, { ...requestA.request, ...withLens }, ready(setA));
  const searchB = useSearch(scope, { ...requestB.request, ...withLens }, ready(setB));
  const pack = useContextPack(
    scope,
    {
      search: question ?? '',
      ...withLens,
      ...(budget === undefined ? {} : { budget_tokens: budget }),
    },
    question !== undefined,
  );

  const comparison = useMemo(
    () =>
      searchA.data === undefined || searchB.data === undefined
        ? null
        : compareResults(searchA.data.results, searchB.data.results),
    [searchA.data, searchB.data],
  );
  const involved = useMemo(
    () => involvedRefs(searchA.data?.results, searchB.data?.results, pack.data),
    [searchA.data, searchB.data, pack.data],
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const asked = text.trim();
    if (asked === '') return;
    const tokens = Number(budgetText);
    onAsk(asked, Number.isInteger(tokens) && tokens >= 1 ? tokens : undefined);
  };

  const embeddingInfo = schema.data?.search.embedding;

  return (
    <>
      <PageHeader
        title={T.title}
        description={T.description}
        docs={T.docs}
        actions={
          guideUrl === null ? undefined : (
            <Link
              to="/$domain/explore"
              params={{ domain: scope.domain }}
              search={scopeSearch}
              className="text-sm text-accent hover:underline"
            >
              {T.askGuide}
            </Link>
          )
        }
      />

      <div className="mb-6">
        <ExampleQuestions
          examples={examples}
          label={T.question.examples}
          className="mb-3"
          onPick={setText}
        />
        <form onSubmit={submit}>
          <FormRow>
            <Input
              label={T.question.label}
              value={text}
              placeholder={T.question.placeholder}
              onChange={(event) => {
                setText(event.target.value);
              }}
              className="min-w-0 flex-1 basis-64"
            />
            {lenses.length > 0 && (
              <Select
                label={T.lens.label}
                hint={T.lens.hint}
                value={lens}
                options={[
                  { value: '', label: T.lens.none },
                  ...lenses.map((l) => ({ value: l.api_name, label: l.display_name })),
                ]}
                onChange={(event) => {
                  setLens(event.target.value);
                }}
                className="w-56"
              />
            )}
            <Input
              label={T.budget.label}
              hint={T.budget.hint}
              inputMode="numeric"
              value={budgetText}
              onChange={(event) => {
                setBudgetText(event.target.value);
              }}
              className="w-44"
            />
            <FormAction>
              <Button type="submit" variant="primary">
                {T.question.ask}
              </Button>
            </FormAction>
          </FormRow>
        </form>
      </div>

      <Section title={T.vector.title}>
        {guideUrl === null ? (
          <p className="mb-2 text-sm text-muted">
            {T.vector.manual(embeddingInfo?.model ?? null, embeddingInfo?.dims ?? null)}
          </p>
        ) : embedding.isError ? (
          <p role="alert" className="mb-2 text-sm text-danger">
            {T.vector.failed(embedding.error.message)}
          </p>
        ) : (
          <p role="status" className="mb-2 text-sm text-muted">
            {waiting
              ? T.vector.embedding
              : embedding.data === undefined
                ? T.vector.embedsOnAsk
                : T.vector.embedded(embedding.data.model, embedding.data.vector.length)}
          </p>
        )}
        {(guideUrl === null || embedding.isError) && (
          <Textarea
            label={T.vector.pasteLabel}
            value={pasted}
            error={manual.error ?? undefined}
            hint={manual.vector === null ? undefined : T.vector.dimensions(manual.vector.length)}
            placeholder={T.vector.pastePlaceholder}
            onChange={(event) => {
              setPasted(event.target.value);
            }}
          />
        )}
      </Section>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <ParamsForm
          name="A"
          params={setA}
          errors={requestA.errors}
          kinds={schema.data?.kinds ?? []}
          hasVector={vector !== null}
          onChange={setSetA}
        />
        <ParamsForm
          name="B"
          params={setB}
          errors={requestB.errors}
          kinds={schema.data?.kinds ?? []}
          hasVector={vector !== null}
          onChange={setSetB}
        />
      </div>

      <Section title={T.results.title}>
        {comparison !== null && (
          <p role="status" className="mb-2 text-sm">
            {T.results.summary(
              comparison.shared,
              comparison.onlyA,
              comparison.onlyB,
              comparison.reordered,
            )}
          </p>
        )}
        <div className="grid gap-4 lg:grid-cols-2">
          <ResultsColumn name="A" domain={scope.domain} query={searchA} comparison={comparison} />
          <ResultsColumn name="B" domain={scope.domain} query={searchB} comparison={comparison} />
        </div>
      </Section>

      <Section title={T.pack.title}>
        <PackPanel domain={scope.domain} question={question} query={pack} />
      </Section>

      <Section title={T.involved.title}>
        <QueryBoundary query={schema}>
          {(data) => <LabCanvas involved={involved} kinds={data.kinds} />}
        </QueryBoundary>
      </Section>
    </>
  );
}
