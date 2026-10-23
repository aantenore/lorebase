import { Link } from '@tanstack/react-router';
import { useEffect, useState, type FormEvent } from 'react';
import { useSchema } from '@/api/queries';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { explore as exploreMessages } from '@/messages/explore';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';
import { LabCanvas } from '@/features/retrieval-lab/lab-canvas';
import { Button } from '@/ui/button';
import { Input, Textarea } from '@/ui/field';
import { Badge, FormAction, FormRow, type BadgeTone } from '@/ui/layout';
import { compactArgs, type Citation } from './events';
import { useExploreTransport } from './guide-auth';
import { ExampleQuestions, useDomainExamples } from './domain-examples';
import { useExploration, type ExplorationState, type StepEvent } from './use-exploration';

/** Session ids Keeper accepts in `X-Lorebase-Session` (docs/agent-trace.md); others get no link. */
const SESSION_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const MAX_STEPS_LIMIT = 50;
const T = exploreMessages;

/**
 * The Explore panel: ask the assistant a question about the Domain and watch the steps it
 * takes as they stream in. Every text from the stream is model output that may quote graph
 * data, so it is rendered as plain text; only the answer's citations become links, to Record
 * pages of this app, and only when the Kind is one of the Domain's.
 */
export function ExplorePanel() {
  const scope = useScope();
  const { explore } = useConfig();
  const transport = useExploreTransport();
  const { state, start, stop } = useExploration(transport, explore.timeoutMs);
  const [question, setQuestion] = useState('');
  const [stepsText, setStepsText] = useState(explore.maxSteps.toString());
  const retryIn = useRetryCountdown(state.failure?.retryAt ?? null);
  const examples = useDomainExamples();

  if (transport === null) return null;

  const steps = Number(stepsText);
  const stepsValid = Number.isInteger(steps) && steps >= 1 && steps <= MAX_STEPS_LIMIT;
  const running = state.status === 'running';
  const canAsk = !running && retryIn === 0 && stepsValid;

  const ask = (text: string) => {
    const asked = text.trim();
    if (asked === '' || !canAsk) return;
    void start({
      domain: scope.domain,
      branch: scope.branch === scope.domainInfo.default_branch ? null : scope.branch,
      question: asked,
      max_steps: steps,
    });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    ask(question);
  };

  return (
    <section aria-label={T.title} className="mb-6 rounded-md border border-line bg-surface p-4">
      <ExampleQuestions
        examples={examples}
        label={T.examples}
        disabled={!canAsk}
        className="mb-3"
        onPick={(picked) => {
          setQuestion(picked);
          ask(picked);
        }}
      />

      <form onSubmit={submit}>
        <FormRow>
          <Textarea
            label={T.question}
            value={question}
            rows={2}
            placeholder={T.placeholder}
            onChange={(event) => {
              setQuestion(event.target.value);
            }}
            className="min-w-0 flex-1 basis-64"
          />
          <Input
            label={T.maxSteps}
            inputMode="numeric"
            value={stepsText}
            error={stepsValid ? undefined : T.stepsRange(MAX_STEPS_LIMIT)}
            onChange={(event) => {
              setStepsText(event.target.value);
            }}
            className="w-28"
          />
          <FormAction>
            {running ? (
              <Button variant="danger" onClick={stop}>
                {T.stop}
              </Button>
            ) : (
              <Button type="submit" variant="primary" disabled={!canAsk || question.trim() === ''}>
                {T.submit}
              </Button>
            )}
          </FormAction>
        </FormRow>
      </form>

      <p className="mt-2 text-xs text-muted">
        {T.limits(stepsValid ? steps : explore.maxSteps, state.model)}
      </p>

      <Status state={state} retryIn={retryIn} />
      {state.steps.length > 0 && <StepList steps={state.steps} />}
      {state.answer !== null && <AnswerView state={state} />}
    </section>
  );
}

/** Seconds left before a refused request may be repeated; 0 when it may. */
function useRetryCountdown(retryAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (retryAt === null) return;
    const tick = () => {
      setNow(Date.now());
    };
    const first = setTimeout(tick, 0);
    const timer = setInterval(tick, 1000);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [retryAt]);
  return retryAt === null ? 0 : Math.max(0, Math.ceil((retryAt - now) / 1000));
}

function Status({ state, retryIn }: { state: ExplorationState; retryIn: number }) {
  if (state.failure !== null) {
    return (
      <p role="alert" className="mt-3 text-sm text-danger">
        {state.failure.message}
        {retryIn > 0 ? T.status.retryIn(retryIn) : ''}
      </p>
    );
  }
  const text =
    state.status === 'running'
      ? T.status.running(state.steps.filter((s) => s.type === 'tool_call').length)
      : state.status === 'stopped'
        ? T.status.stopped
        : state.status === 'done' && state.done !== null
          ? T.status.done(
              state.done.steps,
              (state.done.elapsed_ms / 1000).toFixed(1),
              state.done.tokens.input + state.done.tokens.output,
            )
          : null;
  return text === null ? null : (
    <p role="status" className="mt-3 text-sm text-muted">
      {text}
    </p>
  );
}

const KIND_LABEL: Record<StepEvent['type'], { label: string; tone: BadgeTone }> = {
  thought: { label: T.stepKinds.thought, tone: 'neutral' },
  tool_call: { label: T.stepKinds.tool_call, tone: 'accent' },
  tool_result: { label: T.stepKinds.tool_result, tone: 'success' },
  error: { label: T.stepKinds.error, tone: 'danger' },
};

function StepText({ event }: { event: StepEvent }) {
  switch (event.type) {
    case 'thought':
      return <span className="whitespace-pre-wrap italic">{event.text}</span>;
    case 'tool_call':
      return (
        <span>
          <code className="font-medium">{event.tool}</code>{' '}
          <code className="break-all text-muted">{compactArgs(event.args)}</code>
        </span>
      );
    case 'tool_result':
      return (
        <span className={event.ok ? undefined : 'text-danger'}>
          <code>{event.tool}</code>: <span className="whitespace-pre-wrap">{event.summary}</span>
          {event.refs.length > 0 && (
            <span className="text-muted"> {T.records(event.refs.length)}</span>
          )}
        </span>
      );
    case 'error':
      return (
        <span className="whitespace-pre-wrap text-danger">
          {event.code}: {event.message}
        </span>
      );
  }
}

function StepList({ steps }: { steps: StepEvent[] }) {
  return (
    <ol aria-label={T.steps} className="mt-3 flex flex-col gap-1 text-sm">
      {steps.map((event, index) => {
        const meta = KIND_LABEL[event.type];
        return (
          <li key={index} className="flex items-start gap-2">
            <span className="w-8 shrink-0 text-right text-xs text-muted tabular-nums">
              {'step' in event && event.step > 0 ? event.step : ''}
            </span>
            <Badge tone={meta.tone}>{meta.label}</Badge>
            <StepText event={event} />
          </li>
        );
      })}
    </ol>
  );
}

function AnswerView({ state }: { state: ExplorationState }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const schema = useSchema(scope);
  const [showCanvas, setShowCanvas] = useState(false);
  const answer = state.answer;
  if (answer === null) return null;
  const kinds = schema.data?.kinds;
  const known = (c: Citation) => kinds === undefined || kinds.some((k) => k.api_name === c.kind);
  const linkable = answer.citations.filter(known);
  const sessionId =
    state.sessionId !== null && SESSION_ID.test(state.sessionId) ? state.sessionId : null;

  return (
    <section
      aria-label={T.answer.region}
      className="mt-4 rounded-md border border-line bg-surface-2 p-3"
    >
      <h3 className="mb-1 text-sm font-semibold">
        {answer.found ? T.answer.found : T.answer.notFound}
      </h3>
      <p className="text-sm whitespace-pre-wrap">{answer.text}</p>
      {answer.citations.length > 0 && (
        <>
          <h4 className="mt-3 mb-1 text-xs font-semibold text-muted">{T.answer.cited}</h4>
          <ul aria-label={T.answer.cited} className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
            {answer.citations.map((citation) => (
              <li key={citation.ref}>
                {known(citation) ? (
                  <RecordLink domain={scope.domain} recordRef={citation.ref} />
                ) : (
                  <span className="text-muted">{citation.ref}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        {sessionId !== null && (
          <Link
            to="/$domain/agent-activity"
            params={{ domain: scope.domain }}
            search={{ ...scopeSearch, session: sessionId }}
            className="text-accent hover:underline"
          >
            {T.answer.viewTrace}
          </Link>
        )}
        {linkable.length > 0 && kinds !== undefined && (
          <Button
            size="sm"
            aria-pressed={showCanvas}
            onClick={() => {
              setShowCanvas((shown) => !shown);
            }}
          >
            {showCanvas ? T.answer.hideCanvas : T.answer.showCanvas}
          </Button>
        )}
      </div>
      {showCanvas && kinds !== undefined && (
        <div className="mt-3">
          <LabCanvas
            involved={linkable.map((c) => c.ref)}
            kinds={kinds}
            label={retrievalLabMessages.involved.citedLabel}
            caption={retrievalLabMessages.involved.citedCaption}
          />
        </div>
      )}
    </section>
  );
}
