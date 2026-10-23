import { useState } from 'react';
import { useSchema, useTraceSessions, useTraceTimeline } from '@/api/queries';
import { useScope } from '@/app/scope';
import { agentActivity as agentActivityMessages } from '@/messages/agent-activity';
import { common as commonMessages } from '@/messages/common';
import { Button } from '@/ui/button';
import { Badge, EmptyState, FormAction, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { INFERRED_NOTE, InferredBadge } from './proposal-link';
import { SessionsList, ActorFilter } from './sessions-list';
import { Timeline } from './timeline';
import { TraceCanvas } from './trace-canvas';

function SessionTimeline({
  session,
  actor,
  step,
  onStep,
}: {
  session: string;
  actor: string | undefined;
  step: number | undefined;
  onStep: (step: number) => void;
}) {
  const scope = useScope();
  const timeline = useTraceTimeline(session, actor);
  const schema = useSchema(scope);

  return (
    <Section title={agentActivityMessages.timeline.title(session)}>
      <QueryBoundary query={timeline}>
        {(data) => {
          const last = data.steps[data.steps.length - 1]?.step ?? 1;
          const selected = Math.min(Math.max(step ?? last, 1), last);
          return (
            <>
              {data.steps.some((s) => s.inferred) && (
                <p className="mb-2 flex items-center gap-2 text-xs text-muted">
                  <InferredBadge />
                  {INFERRED_NOTE}.
                </p>
              )}
              {data.truncated && (
                <p className="mb-2 text-xs text-warning">
                  {agentActivityMessages.timeline.truncated}
                </p>
              )}
              {data.steps.length === 0 ? (
                <EmptyState>{agentActivityMessages.timeline.empty}</EmptyState>
              ) : (
                <div className="grid gap-4 xl:grid-cols-2">
                  <Timeline steps={data.steps} domain={scope.domain} current={selected} />
                  <div className="xl:sticky xl:top-0 xl:self-start">
                    <TraceCanvas
                      steps={data.steps}
                      kinds={schema.data?.kinds ?? []}
                      selected={selected}
                      onSelect={onStep}
                    />
                  </div>
                </div>
              )}
            </>
          );
        }}
      </QueryBoundary>
    </Section>
  );
}

/**
 * Agent activity: the sessions Keeper traced, and for one of them the steps in order (calls with
 * what they asked and got, notes the agent left) next to a Canvas overlay of the Records and
 * Links the session touched. Installation-level data, shown under the Domain on screen.
 */
export function AgentActivityPage({
  session,
  actor,
  step,
  onActor,
  onStep,
}: {
  session: string | undefined;
  actor: string | undefined;
  step: number | undefined;
  onActor: (actor: string | undefined) => void;
  onStep: (step: number) => void;
}) {
  const scope = useScope();
  const [thisDomain, setThisDomain] = useState(false);
  const sessions = useTraceSessions({
    ...(actor === undefined ? {} : { actor }),
    ...(thisDomain ? { domain_id: scope.domain } : {}),
  });
  const pages = sessions.data?.pages ?? [];
  const items = pages.flatMap((page) => page.items);
  const enabled = pages[0]?.enabled ?? true;

  return (
    <>
      <PageHeader
        title={agentActivityMessages.title}
        description={agentActivityMessages.description}
        docs={agentActivityMessages.docs}
        actions={<Badge tone="accent">{agentActivityMessages.allDomains}</Badge>}
      />
      {!enabled && (
        <p role="status" className="mb-4 rounded-md border border-warning p-3 text-sm text-warning">
          {agentActivityMessages.tracingOff}
        </p>
      )}
      <Section title={agentActivityMessages.sessions.title}>
        <div className="mb-3 flex flex-wrap items-start gap-x-4 gap-y-3">
          <ActorFilter key={actor ?? ''} actor={actor} onActor={onActor} />
          <FormAction>
            <label className="flex h-9 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={thisDomain}
                onChange={(event) => {
                  setThisDomain(event.target.checked);
                }}
              />
              {agentActivityMessages.sessions.thisDomain}
            </label>
          </FormAction>
        </div>
        {sessions.isPending ? (
          <p role="status" className="text-muted">
            {commonMessages.loading}
          </p>
        ) : sessions.isError ? (
          <p role="alert" className="text-danger">
            {sessions.error.message}
          </p>
        ) : (
          <>
            <SessionsList domain={scope.domain} sessions={items} selected={session} />
            {sessions.hasNextPage && (
              <Button
                className="mt-2"
                disabled={sessions.isFetchingNextPage}
                onClick={() => {
                  void sessions.fetchNextPage();
                }}
              >
                {agentActivityMessages.loadMore}
              </Button>
            )}
          </>
        )}
      </Section>
      {session === undefined ? (
        <p className="text-sm text-muted">{agentActivityMessages.pickSession}</p>
      ) : (
        <SessionTimeline session={session} actor={actor} step={step} onStep={onStep} />
      )}
    </>
  );
}
