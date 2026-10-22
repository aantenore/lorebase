import { Link } from '@tanstack/react-router';
import type { TraceStep } from '@/api/types';
import { Badge, type BadgeTone } from '@/ui/layout';
import { cn } from '@/ui/cn';
import { ProposalLink } from './proposal-link';
import {
  budgetShare,
  clockTime,
  correctionText,
  countsSummary,
  parseResult,
  queryText,
  type BudgetView,
} from './step-view';
import { stepRefs } from './trace-graph';

/** The step number the agent gave its note, when it did. */
function agentStep(step: TraceStep): number | null {
  const value = step.result?.['agent_step'];
  return typeof value === 'number' ? value : null;
}

const statusTone = (status: number): BadgeTone =>
  status >= 500 ? 'danger' : status >= 400 ? 'warning' : 'success';

function Budget({ budget }: { budget: BudgetView }) {
  const percent = Math.round(budgetShare(budget) * 100);
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span>
        Budget {budget.usedTokens} / {budget.limitTokens} tokens
      </span>
      <span aria-hidden="true" className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-2">
        <span
          className={cn('block h-full', budget.overBudget ? 'bg-danger' : 'bg-accent')}
          style={{ width: `${percent.toString()}%` }}
        />
      </span>
      {budget.overBudget && <Badge tone="danger">over budget</Badge>}
      {budget.capped && <Badge tone="warning">capped</Badge>}
    </div>
  );
}

function Json({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted">{label}</p>
      <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-line bg-surface-2 p-2 font-mono text-xs whitespace-pre-wrap">
        {value}
      </pre>
    </div>
  );
}

function CallBody({ step, domain }: { step: TraceStep; domain: string }) {
  const result = parseResult(step.result);
  const counts = countsSummary(result.counts);
  const query = queryText(step.query);
  const refs = stepRefs(step);
  const touched = step.refs?.truncated === true;
  return (
    <div className="mt-2 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {step.tool != null && <Badge tone="accent">{step.tool}</Badge>}
        <code className="font-mono text-xs">
          {step.method} {step.route}
        </code>
        {step.status != null && <Badge tone={statusTone(step.status)}>{step.status}</Badge>}
        {step.latency_ms != null && (
          <span className="text-xs text-muted">{step.latency_ms} ms</span>
        )}
        {step.cache != null && <Badge>cache {step.cache}</Badge>}
        {step.branch != null && <Badge>{step.branch}</Badge>}
        {result.mode !== null && <Badge>{result.mode}</Badge>}
      </div>
      {(counts !== '' || refs.records.length > 0 || refs.links.length > 0) && (
        <p className="text-xs text-muted">
          {counts !== '' && <>Result: {counts}. </>}
          {(refs.records.length > 0 || refs.links.length > 0) && (
            <>
              Touched {refs.records.length} Records and {refs.links.length} Links
              {touched ? ' (more than listed)' : ''}.
            </>
          )}
        </p>
      )}
      {result.corrections.length > 0 && (
        <p className="text-xs">
          Corrections:{' '}
          {result.corrections.map((c, index) => (
            <span key={c.word}>
              {index > 0 && '; '}
              <code className="font-mono">{correctionText(c)}</code>
            </span>
          ))}
        </p>
      )}
      {result.budget !== null && <Budget budget={result.budget} />}
      {(result.truncated || result.hasMore) && (
        <p className="text-xs text-warning">
          {result.truncated ? 'The answer was cut short.' : 'There was more to read.'}
        </p>
      )}
      {query !== null && <Json label="Query" value={query} />}
      {step.explain != null && Object.keys(step.explain).length > 0 && (
        <Json label="Plan" value={JSON.stringify(step.explain, null, 2)} />
      )}
      {step.proposal_id != null && (
        <p className="text-sm">
          Opened <ProposalLink domain={step.domain_id ?? domain} proposal={step.proposal_id} />
        </p>
      )}
    </div>
  );
}

/** One step: a call with what it asked and got, or a note the agent left, as plain text. */
function StepCard({
  step,
  domain,
  current,
}: {
  step: TraceStep;
  domain: string;
  current: boolean;
}) {
  return (
    <li
      aria-current={current ? 'step' : undefined}
      className={cn(
        'rounded-md border bg-surface p-3',
        current ? 'border-accent' : 'border-line',
        step.type === 'note' && 'border-dashed',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Link
          to="/$domain/agent-activity"
          params={{ domain }}
          search={(previous) => ({ ...previous, step: step.step })}
          aria-label={`Show step ${step.step.toString()} on the Canvas`}
          className="font-mono text-xs font-semibold text-accent hover:underline"
        >
          #{step.step}
        </Link>
        <span className="text-xs text-muted">{clockTime(step.at)}</span>
        <Badge tone={step.type === 'note' ? 'warning' : 'neutral'}>{step.type}</Badge>
        {step.type === 'note' && agentStep(step) !== null && (
          <span className="text-xs text-muted">agent step {agentStep(step)}</span>
        )}
        {step.domain_id != null && <span className="text-xs text-muted">{step.domain_id}</span>}
        <span className="text-xs text-muted">{step.actor}</span>
      </div>
      {step.withheld === true && (
        <p className="mt-1 text-xs text-muted">
          Query, corrections and note text of this step are withheld: they may hold values you would
          see masked.
        </p>
      )}
      {step.type === 'note' ? (
        // The note is the agent's own text: shown as text, never parsed as markup.
        <p className="mt-2 text-sm whitespace-pre-wrap">{step.note}</p>
      ) : (
        <CallBody step={step} domain={domain} />
      )}
    </li>
  );
}

/** The steps of a session in order; the one the Canvas overlay stands at is marked. */
export function Timeline({
  steps,
  domain,
  current,
}: {
  steps: TraceStep[];
  domain: string;
  current: number;
}) {
  return (
    <ol aria-label="Session timeline" className="flex flex-col gap-2">
      {steps.map((step) => (
        <StepCard key={step.step} step={step} domain={domain} current={step.step === current} />
      ))}
    </ol>
  );
}
