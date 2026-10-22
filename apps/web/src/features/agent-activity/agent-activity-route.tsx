import { getRouteApi, useNavigate } from '@tanstack/react-router';
import { AgentActivityPage } from './agent-activity-page';

const route = getRouteApi('/$domain/agent-activity');

/** The Agent activity screen bound to its URL state; loaded lazily by the route. */
export function AgentActivityRoute() {
  const { session, actor, step } = route.useSearch();
  const navigate = useNavigate();
  return (
    <AgentActivityPage
      session={session}
      actor={actor}
      step={step}
      onActor={(next) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, actor: next, session: undefined, step: undefined }),
          replace: true,
        });
      }}
      onStep={(next) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, step: next }),
          replace: true,
        });
      }}
    />
  );
}
