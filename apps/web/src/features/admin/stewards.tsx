import { common } from '@/messages/common';
import { Badge } from '@/ui/layout';
import { usePrincipalNames } from './principal-names';

const text = common.stewards;

/** Assignees by display name, with a hint when the list is cut short. */
function Assignees({ assignees, truncated }: { assignees: string[]; truncated: boolean }) {
  const nameOf = usePrincipalNames();
  return (
    <span aria-label={text.assignees}>
      {assignees.map(nameOf).join(', ')}
      {truncated && <span className="text-muted"> {text.more}</span>}
    </span>
  );
}

/** Steward roles of a Proposal or candidate and, when the caller may see them, its assignees. */
export function Stewards({
  roles,
  assignees,
  truncated,
}: {
  roles?: string[] | undefined;
  assignees?: string[] | null | undefined;
  truncated?: boolean | undefined;
}) {
  if (roles === undefined || roles.length === 0) return null;
  return (
    <span className="flex flex-wrap items-center gap-1 text-xs">
      <span role="group" aria-label={text.roles} className="flex flex-wrap gap-1">
        {roles.map((role) => (
          <Badge key={role}>{role}</Badge>
        ))}
      </span>
      {assignees != null && assignees.length > 0 && (
        <Assignees assignees={assignees} truncated={truncated === true} />
      )}
    </span>
  );
}
