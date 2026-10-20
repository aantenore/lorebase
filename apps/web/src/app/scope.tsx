import { useSearch } from '@tanstack/react-router';
import { createContext, use, useMemo, type ReactNode } from 'react';
import { useBranch, useDomain, type QueryScope } from '@/api/queries';
import type { Branch, Domain } from '@/api/types';

/** Search params every screen under a Domain understands. */
export interface ScopeSearch {
  /** Branch name; the Domain default Branch when absent. */
  branch?: string | undefined;
  /** Revision id or tag for time-travel reads. */
  at?: string | undefined;
}

/** Read a search param as non-empty text; the router may have parsed numbers. */
export function readText(value: unknown): string | undefined {
  if (typeof value === 'string' && value !== '') return value;
  if (typeof value === 'number') return String(value);
  return undefined;
}

export function validateScopeSearch(raw: Record<string, unknown>): ScopeSearch {
  return { branch: readText(raw['branch']), at: readText(raw['at']) };
}

export interface Scope extends QueryScope {
  domainInfo: Domain;
  branchInfo: Branch;
}

const ScopeContext = createContext<Scope | null>(null);

/**
 * Resolves the Domain and Branch named by the URL and exposes them, with the Branch
 * head Revision, to every screen. Children render only when both exist, so screens
 * never deal with a missing Branch.
 */
export function ScopeProvider({
  domain,
  search,
  children,
}: {
  domain: string;
  search: ScopeSearch;
  children: ReactNode;
}) {
  const domainQuery = useDomain(domain);
  const branchName = search.branch ?? domainQuery.data?.default_branch;
  const branchQuery = useBranch(domain, branchName);

  const value = useMemo<Scope | null>(() => {
    if (domainQuery.data === undefined || branchQuery.data === undefined) return null;
    return {
      domain,
      branch: branchQuery.data.name,
      head: branchQuery.data.head,
      ...(search.at === undefined ? {} : { at: search.at }),
      domainInfo: domainQuery.data,
      branchInfo: branchQuery.data,
    };
  }, [domain, domainQuery.data, branchQuery.data, search.at]);

  if (domainQuery.isError) {
    return (
      <p role="alert" className="p-6 text-danger">
        {domainQuery.error.message}
      </p>
    );
  }
  if (branchQuery.isError) {
    return (
      <p role="alert" className="p-6 text-danger">
        {branchQuery.error.message}
      </p>
    );
  }
  if (value === null) {
    return (
      <p role="status" className="p-6 text-muted">
        Loading...
      </p>
    );
  }
  return <ScopeContext value={value}>{children}</ScopeContext>;
}

export function useScope(): Scope {
  const scope = use(ScopeContext);
  if (scope === null) throw new Error('useScope must be used under a Domain route.');
  return scope;
}

/** Branch and Revision context from the URL, to carry along when linking to another screen. */
export function useScopeSearch(): ScopeSearch {
  const raw: Record<string, unknown> = useSearch({ strict: false });
  const { branch, at } = validateScopeSearch(raw);
  return useMemo(() => ({ branch, at }), [branch, at]);
}
