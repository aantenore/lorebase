import type { ReactNode } from 'react';
import { ScopeProvider, type ScopeSearch } from './scope';
import { SideNav } from './side-nav';
import { TopBar } from './top-bar';

/** Top bar, left navigation and the scoped content area. */
export function AppShell({
  domain,
  search,
  children,
}: {
  domain: string;
  search: ScopeSearch;
  children: ReactNode;
}) {
  return (
    <div className="flex h-full flex-col">
      <TopBar domain={domain} search={search} />
      <div className="flex min-h-0 flex-1">
        <SideNav domain={domain} />
        <main className="min-w-0 flex-1 overflow-auto p-6">
          <ScopeProvider domain={domain} search={search}>
            {children}
          </ScopeProvider>
        </main>
      </div>
    </div>
  );
}
