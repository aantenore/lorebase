import { Link, useNavigate } from '@tanstack/react-router';
import { useBranches, useDomain } from '@/api/queries';
import { Badge } from '@/ui/layout';
import { Select } from '@/ui/field';
import type { ScopeSearch } from './scope';

const SHORT_REVISION_LENGTH = 8;

/**
 * Branch is first class: the selector sits in the top bar of every screen, shows the
 * head Revision and how far the Branch is behind the default Branch.
 */
export function BranchSwitcher({ domain, search }: { domain: string; search: ScopeSearch }) {
  const navigate = useNavigate();
  const domainQuery = useDomain(domain);
  const branches = useBranches(domain);

  const defaultBranch = domainQuery.data?.default_branch;
  const current = search.branch ?? defaultBranch;
  const selected = branches.data?.items.find((b) => b.name === current);

  const options = (branches.data?.items ?? []).map((b) => ({ value: b.name, label: b.name }));
  if (current !== undefined && !options.some((o) => o.value === current)) {
    options.unshift({ value: current, label: current });
  }

  return (
    <div className="flex items-center gap-2">
      <Select
        label="Branch"
        hideLabel
        value={current ?? ''}
        options={options}
        onChange={(event) => {
          const next = event.target.value;
          void navigate({
            to: '.',
            search: (prev: ScopeSearch) => ({
              ...prev,
              branch: next === defaultBranch ? undefined : next,
            }),
          });
        }}
      />
      {selected !== undefined && (
        <Badge tone="accent">Revision {selected.head.slice(0, SHORT_REVISION_LENGTH)}</Badge>
      )}
      {selected !== undefined && selected.behind_default > 0 && defaultBranch !== undefined && (
        <Badge tone="warning">
          behind {defaultBranch} by {selected.behind_default}
        </Badge>
      )}
      {search.at !== undefined && (
        <Link
          to="."
          search={(prev: ScopeSearch) => ({ ...prev, at: undefined })}
          className="text-xs underline"
        >
          At {search.at.slice(0, SHORT_REVISION_LENGTH)} (back to head)
        </Link>
      )}
    </div>
  );
}
