import { Link } from '@tanstack/react-router';
import { cn } from '@/ui/cn';
import { useScopeSearch } from './scope';

const ITEMS = [
  { to: '/$domain/explorer', label: 'Explorer' },
  { to: '/$domain/canvas', label: 'Canvas' },
  { to: '/$domain/modeler', label: 'Modeler' },
  { to: '/$domain/review', label: 'Review' },
  { to: '/$domain/sources', label: 'Sources' },
  { to: '/$domain/settings', label: 'Settings' },
] as const;

/** Left navigation. Branch and Revision context in the URL survive navigation. */
export function SideNav({ domain }: { domain: string }) {
  const scopeSearch = useScopeSearch();
  return (
    <nav aria-label="Main" className="w-44 shrink-0 border-r border-line bg-surface p-2">
      <ul className="flex flex-col gap-1">
        {ITEMS.map((item) => (
          <li key={item.to}>
            <Link
              to={item.to}
              params={{ domain }}
              search={scopeSearch}
              activeProps={{ 'aria-current': 'page', className: 'bg-surface-2 text-ink' }}
              inactiveProps={{ className: 'text-muted hover:bg-surface-2 hover:text-ink' }}
              className={cn('block rounded-md px-3 py-2 text-sm font-medium')}
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
