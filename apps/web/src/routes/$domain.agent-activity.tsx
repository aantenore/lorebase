import { createFileRoute, lazyRouteComponent } from '@tanstack/react-router';
import { validateActivitySearch } from '@/features/agent-activity/search';

/**
 * Agent activity. The timeline of a session is at `/<domain>/agent-activity?session=<session_id>`
 * (optionally `&actor=<principal>&step=<n>`). The trace is installation-level; the screen lives
 * under a Domain for the shell, the Branch context and the links to Records and Proposals.
 */
export const Route = createFileRoute('/$domain/agent-activity')({
  validateSearch: validateActivitySearch,
  component: lazyRouteComponent(
    () => import('@/features/agent-activity/agent-activity-route'),
    'AgentActivityRoute',
  ),
});
