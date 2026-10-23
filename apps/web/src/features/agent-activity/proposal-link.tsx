import { Link } from '@tanstack/react-router';
import { useScopeSearch } from '@/app/scope';
import { agentActivity as agentActivityMessages } from '@/messages/agent-activity';
import { Badge } from '@/ui/layout';

/** Marks a session Keeper grouped itself because its calls carried no session id. */
export function InferredBadge() {
  return (
    <span title={INFERRED_NOTE} className="ml-2">
      <Badge tone="warning">{agentActivityMessages.timeline.inferred}</Badge>
    </span>
  );
}

export const INFERRED_NOTE = agentActivityMessages.timeline.inferredNote;

/** Link to the Review screen of a Proposal a session opened. */
export function ProposalLink({ domain, proposal }: { domain: string; proposal: string }) {
  const scopeSearch = useScopeSearch();
  return (
    <Link
      to="/$domain/review/$proposal"
      params={{ domain, proposal }}
      search={scopeSearch}
      className="text-accent hover:underline"
    >
      {agentActivityMessages.timeline.proposal(proposal)}
    </Link>
  );
}
