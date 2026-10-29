import { Link } from '@tanstack/react-router';
import { useConfig } from '@/config/context';
import { shell as shellMessages } from '@/messages/shell';
import { cn } from '@/ui/cn';
import { useScopeSearch } from './scope';

const NAV = shellMessages.nav;
const ITEMS = [
  { to: '/$domain/explorer', label: NAV.explorer },
  { to: '/$domain/canvas', label: NAV.canvas },
  { to: '/$domain/modeler', label: NAV.modeler },
  { to: '/$domain/review', label: NAV.review },
  { to: '/$domain/reconcile', label: NAV.reconcile },
  { to: '/$domain/sources', label: NAV.sources },
  { to: '/$domain/agent-activity', label: NAV.agentActivity },
  { to: '/$domain/retrieval-lab', label: NAV.retrievalLab },
  { to: '/$domain/explore', label: NAV.explore },
  { to: '/$domain/documents', label: NAV.documents },
  { to: '/$domain/settings', label: NAV.settings },
] as const;

/**
 * Main navigation: a column on the left from tablet width up, a scrolling row under the top bar
 * on phones. Branch and Revision context in the URL survive navigation.
 */
export function SideNav({ domain }: { domain: string }) {
  const scopeSearch = useScopeSearch();
  const { apiMode, guideUrl } = useConfig();
  // Explore needs a guide service, except against the mock API, which plays scripted answers.
  const items = ITEMS.filter(
    (item) => item.to !== '/$domain/explore' || apiMode === 'mock' || guideUrl !== null,
  );
  return (
    <nav
      aria-label={NAV.label}
      className="shrink-0 overflow-x-auto border-b border-line bg-surface p-2 md:w-44 md:overflow-x-visible md:border-r md:border-b-0"
    >
      <ul className="flex gap-1 md:flex-col">
        {items.map((item) => (
          <li key={item.to}>
            <Link
              to={item.to}
              params={{ domain }}
              search={scopeSearch}
              activeProps={{ 'aria-current': 'page', className: 'bg-surface-2 text-ink' }}
              inactiveProps={{ className: 'text-muted hover:bg-surface-2 hover:text-ink' }}
              className={cn('block rounded-md px-3 py-2 text-sm font-medium whitespace-nowrap')}
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
