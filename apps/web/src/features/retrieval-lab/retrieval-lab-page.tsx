import { useQuery } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { useContextPack, useSchema, useSearch } from '@/api/queries';
import { useScope } from '@/app/scope';
import { useConfig } from '@/config/context';
import { useGuideAuth } from '@/features/explore/guide-auth';
import { Button } from '@/ui/button';
import { Input, Textarea } from '@/ui/field';
import { PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { ExplorePanelSlot } from './explore-panel-slot';
import { embed, parseVector } from './guide-client';
import { LabCanvas } from './lab-canvas';
import { involvedRefs } from './lab-graph';
import { PackPanel } from './pack-panel';
import { ParamsForm } from './params-form';
import { defaultParams, needsVector, toRequest, type LabParams } from './params';
import { compareResults } from './result-diff';
import { ResultsColumn } from './results-column';

/** A set that wants a vector searches by words while there is none. */
const effective = (params: LabParams, vector: number[] | null): LabParams =>
  needsVector(params.mode) && vector === null ? { ...params, mode: 'lexical' } : params;

/**
 * Retrieval lab: ask a question, compare two parameter sets of the Keeper's search side by side,
 * and see the context pack an agent would receive for it, with the Records involved on a Canvas.
 * The query vector for semantic and hybrid search comes from the optional guide service
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
  const { guideUrl, searchLimit } = useConfig();
  const guideAuth = useGuideAuth();
  const schema = useSchema(scope);

  const [text, setText] = useState(question ?? '');
  const [budgetText, setBudgetText] = useState(budget === undefined ? '' : budget.toString());
  const [setA, setSetA] = useState<LabParams>(defaultParams);
  const [setB, setSetB] = useState<LabParams>(() => ({ ...defaultParams(), mode: 'hybrid' }));
  const [pasted, setPasted] = useState('');

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
  const searchA = useSearch(scope, requestA.request, ready(setA));
  const searchB = useSearch(scope, requestB.request, ready(setB));
  const pack = useContextPack(
    scope,
    { search: question ?? '', ...(budget === undefined ? {} : { budget_tokens: budget }) },
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
        title="Retrieval lab"
        description={`Try the search of ${scope.domainInfo.display_name} with two parameter sets, and see the context an agent would get.`}
      />

      <form onSubmit={submit} className="mb-4 flex flex-wrap items-end gap-3">
        <Input
          label="Question"
          value={text}
          placeholder="Which suppliers deliver bearings to Torino?"
          onChange={(event) => {
            setText(event.target.value);
          }}
          className="min-w-80 flex-1"
        />
        <Input
          label="Pack budget (tokens)"
          hint="Empty: Keeper's default"
          inputMode="numeric"
          value={budgetText}
          onChange={(event) => {
            setBudgetText(event.target.value);
          }}
          className="w-44"
        />
        <Button type="submit" variant="primary">
          Ask
        </Button>
      </form>

      <Section title="Query vector">
        {guideUrl === null ? (
          <p className="mb-2 text-sm text-muted">
            No guide service is configured (guideUrl), so the question is searched by words only.
            Semantic and hybrid search need a query vector
            {embeddingInfo == null
              ? '.'
              : ` of ${embeddingInfo.model} (${embeddingInfo.dims.toString()} dimensions).`}{' '}
            Paste one to use them.
          </p>
        ) : embedding.isError ? (
          <p role="alert" className="mb-2 text-sm text-danger">
            {embedding.error.message} Paste a vector to use semantic and hybrid search.
          </p>
        ) : (
          <p role="status" className="mb-2 text-sm text-muted">
            {waiting
              ? 'Asking the guide service to embed the question...'
              : embedding.data === undefined
                ? 'The guide service embeds the question when you ask.'
                : `Embedded by the guide service${embedding.data.model === undefined ? '' : ` (${embedding.data.model})`}: ${embedding.data.vector.length.toString()} dimensions.`}
          </p>
        )}
        {(guideUrl === null || embedding.isError) && (
          <Textarea
            label="Query vector (JSON array)"
            value={pasted}
            error={manual.error ?? undefined}
            hint={
              manual.vector === null ? undefined : `${manual.vector.length.toString()} dimensions`
            }
            placeholder="[0.12, -0.03, 0.4]"
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

      <Section title="Results">
        {comparison !== null && (
          <p role="status" className="mb-2 text-sm">
            {comparison.shared} in both, {comparison.onlyA} only in A, {comparison.onlyB} only in B,{' '}
            {comparison.reordered} at a different rank.
          </p>
        )}
        <div className="grid gap-4 lg:grid-cols-2">
          <ResultsColumn name="A" domain={scope.domain} query={searchA} comparison={comparison} />
          <ResultsColumn name="B" domain={scope.domain} query={searchB} comparison={comparison} />
        </div>
      </Section>

      <Section title="Context pack">
        <PackPanel domain={scope.domain} question={question} query={pack} />
      </Section>

      <Section title="Records involved">
        <QueryBoundary query={schema}>
          {(data) => <LabCanvas involved={involved} kinds={data.kinds} />}
        </QueryBoundary>
      </Section>

      <ExplorePanelSlot />
    </>
  );
}
