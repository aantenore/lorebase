import { Link } from '@tanstack/react-router';
import { useScopeSearch } from '@/app/scope';
import { Badge } from '@/ui/layout';

/** Marks a session Keeper grouped itself because its calls carried no session id. */
export function InferredBadge() {
  return (
    <span title={INFERRED_NOTE} className="ml-2">
      <Badge tone="warning">inferred</Badge>
    </span>
  );
}

export const INFERRED_NOTE = 'Grouped heuristically: these calls carried no session id';

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
      Proposal #{proposal}
    </Link>
  );
}
