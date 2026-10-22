import { Link } from '@tanstack/react-router';
import { useEffect, useId, useState, type FormEvent } from 'react';
import { useSchema } from '@/api/queries';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { LabCanvas } from '@/features/retrieval-lab/lab-canvas';
import { Button } from '@/ui/button';
import { Input, Textarea } from '@/ui/field';
import { Badge, type BadgeTone } from '@/ui/layout';
import { compactArgs, type Citation } from './events';
import { useExploreTransport } from './guide-auth';
import { presetsFor } from './presets';
import { useExploration, type ExplorationState, type StepEvent } from './use-exploration';

/** Session ids Keeper accepts in `X-Lorebase-Session` (docs/agent-trace.md); others get no link. */
const SESSION_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const MAX_STEPS_LIMIT = 50;

/**
 * The Explore panel: ask the guide service a question about the Domain and watch the steps it
 * takes as they stream in. Every text from the stream is model output that may quote graph
 * data, so it is rendered as plain text; only the answer's citations become links, to Record
 * pages of this app, and only when the Kind is one of the Domain's.
 */
export function ExplorePanel({ headingLevel = 2 }: { headingLevel?: 1 | 2 }) {
  const scope = useScope();
  const { explore } = useConfig();
  const transport = useExploreTransport();
  const { state, start, stop } = useExploration(transport, explore.timeoutMs);
  const [question, setQuestion] = useState('');
  const [stepsText, setStepsText] = useState(explore.maxSteps.toString());
  const retryIn = useRetryCountdown(state.failure?.retryAt ?? null);
  const headingId = useId();

  if (transport === null) return null;

  const steps = Number(stepsText);
  const stepsValid = Number.isInteger(steps) && steps >= 1 && steps <= MAX_STEPS_LIMIT;
  const running = state.status === 'running';
  const canAsk = !running && retryIn === 0 && stepsValid;
  const presets = presetsFor(scope.domain);

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

  const Heading = headingLevel === 1 ? 'h1' : 'h2';
  return (
    <section
      aria-labelledby={headingId}
      className="mb-6 rounded-md border border-line bg-surface p-4"
    >
      <Heading
        id={headingId}
        className={headingLevel === 1 ? 'text-xl font-semibold' : 'text-sm font-semibold'}
      >
        Explore
      </Heading>
      <p className="mt-1 mb-3 max-w-3xl text-sm text-muted">
        Ask the guide service a question about {scope.domainInfo.display_name}. It reads the graph
        with your access, step by step, and answers only from Records it was shown.
      </p>

      {presets.length > 0 && (
        <div role="group" aria-label="Preset questions" className="mb-3 flex flex-wrap gap-2">
          {presets.map((preset) => (
            <Button
              key={preset.id}
              size="sm"
              disabled={!canAsk}
              title={preset.question}
              onClick={() => {
                setQuestion(preset.question);
                ask(preset.question);
              }}
            >
              {preset.label}
            </Button>
          ))}
        </div>
      )}

      <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
        <Textarea
          label="Question for the guide"
          value={question}
          rows={2}
          placeholder="Which suppliers deliver bearings to Torino?"
          onChange={(event) => {
            setQuestion(event.target.value);
          }}
          className="min-w-80 flex-1"
        />
        <Input
          label="Max steps"
          inputMode="numeric"
          value={stepsText}
          error={stepsValid ? undefined : `1 to ${MAX_STEPS_LIMIT.toString()}`}
          onChange={(event) => {
            setStepsText(event.target.value);
          }}
          className="w-28"
        />
        {running ? (
          <Button variant="danger" onClick={stop}>
            Stop
          </Button>
        ) : (
          <Button type="submit" variant="primary" disabled={!canAsk || question.trim() === ''}>
            Explore
          </Button>
        )}
      </form>

      <p className="mt-2 text-xs text-muted">
        Settings: at most {stepsValid ? steps : explore.maxSteps} steps (the guide service may allow
        fewer)
        {state.model === null ? '' : `, model ${state.model}`}.
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
        {retryIn > 0 ? ` Try again in ${retryIn.toString()} s.` : ''}
      </p>
    );
  }
  const text =
    state.status === 'running'
      ? `Exploring... ${state.steps.filter((s) => s.type === 'tool_call').length.toString()} steps so far.`
      : state.status === 'stopped'
        ? 'Stopped. The guide service no longer works on this question.'
        : state.status === 'done' && state.done !== null
          ? `Done in ${state.done.steps.toString()} steps, ${(state.done.elapsed_ms / 1000).toFixed(1)} s, ${(state.done.tokens.input + state.done.tokens.output).toString()} tokens.`
          : null;
  return text === null ? null : (
    <p role="status" className="mt-3 text-sm text-muted">
      {text}
    </p>
  );
}

const KIND_LABEL: Record<StepEvent['type'], { label: string; tone: BadgeTone }> = {
  thought: { label: 'thought', tone: 'neutral' },
  tool_call: { label: 'tool call', tone: 'accent' },
  tool_result: { label: 'result', tone: 'success' },
  error: { label: 'error', tone: 'danger' },
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
            <span className="text-muted"> ({event.refs.length} Records)</span>
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
    <ol aria-label="Exploration steps" className="mt-3 flex flex-col gap-1 text-sm">
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
    <section aria-label="Answer" className="mt-4 rounded-md border border-line bg-surface-2 p-3">
      <h3 className="mb-1 text-sm font-semibold">{answer.found ? 'Answer' : 'No answer'}</h3>
      <p className="text-sm whitespace-pre-wrap">{answer.text}</p>
      {answer.citations.length > 0 && (
        <>
          <h4 className="mt-3 mb-1 text-xs font-semibold text-muted">Cited Records</h4>
          <ul aria-label="Cited Records" className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
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
            View trace
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
            {showCanvas ? 'Hide the Canvas' : 'Show cited Records on the Canvas'}
          </Button>
        )}
      </div>
      {showCanvas && kinds !== undefined && (
        <div className="mt-3">
          <LabCanvas
            involved={linkable.map((c) => c.ref)}
            kinds={kinds}
            label="Cited Records"
            caption="Records cited by the answer, drawn with their neighbors."
          />
        </div>
      )}
    </section>
  );
}
