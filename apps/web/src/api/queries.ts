import { useInfiniteQuery, useQueries, useQuery } from '@tanstack/react-query';
import type { PrincipalListParams } from './admin-types';
import { useConfig } from '@/config/context';
import type { DocumentDetail, ExtractionItem } from './extraction-types';
import { useKeeperApi } from './provider';
import type { SourceRunListParams } from './sources-types';
import type {
  BranchName,
  DomainId,
  NeighborhoodRequest,
  PageParams,
  ProposalListParams,
  Query,
  ReadScope,
  RecordListParams,
  RevisionId,
  SearchRequest,
} from './types';

/**
 * Read context for cached queries. `head` is the Branch head Revision at load time:
 * it is part of every cache key, so when a Branch moves, cached reads for the old
 * head are never served as the new one (design 12.1).
 */
export interface QueryScope {
  domain: DomainId;
  branch: BranchName;
  /** Revision id or tag when browsing history; overrides the head. */
  at?: string;
  head: RevisionId;
}

export const toReadScope = (scope: QueryScope): ReadScope => ({
  domain: scope.domain,
  branch: scope.branch,
  ...(scope.at === undefined ? {} : { at: scope.at }),
});

const at = (scope: QueryScope) =>
  ['keeper', scope.domain, scope.branch, scope.at ?? scope.head] as const;

export const keys = {
  domains: () => ['keeper', 'domains'] as const,
  domain: (domain: DomainId) => ['keeper', 'domain', domain] as const,
  branches: (domain: DomainId) => ['keeper', 'branches', domain] as const,
  branch: (domain: DomainId, branch: BranchName) => ['keeper', 'branch', domain, branch] as const,
  log: (domain: DomainId, branch: BranchName, head: RevisionId | undefined) =>
    ['keeper', 'log', domain, branch, head] as const,
  schema: (scope: QueryScope) => [...at(scope), 'schema'] as const,
  records: (scope: QueryScope, kind: string, params: RecordListParams | undefined) =>
    [...at(scope), 'records', kind, params ?? null] as const,
  record: (scope: QueryScope, kind: string, key: string) =>
    [...at(scope), 'record', kind, key] as const,
  links: (scope: QueryScope, kind: string, key: string, relation: string) =>
    [...at(scope), 'links', kind, key, relation] as const,
  lineage: (scope: QueryScope, kind: string, key: string) =>
    [...at(scope), 'lineage', kind, key] as const,
  history: (scope: QueryScope, kind: string, key: string) =>
    [...at(scope), 'history', kind, key] as const,
  query: (scope: QueryScope, query: Query) => [...at(scope), 'query', query] as const,
  search: (scope: QueryScope, request: SearchRequest) => [...at(scope), 'search', request] as const,
  neighborhood: (scope: QueryScope, request: NeighborhoodRequest) =>
    [...at(scope), 'neighborhood', request] as const,
  proposals: (domain: DomainId, params: ProposalListParams | undefined) =>
    ['keeper', 'proposals', domain, params ?? null] as const,
  proposal: (domain: DomainId, id: string) => ['keeper', 'proposal', domain, id] as const,
  proposalDiff: (domain: DomainId, id: string) => ['keeper', 'proposal-diff', domain, id] as const,
  conflicts: (domain: DomainId, id: string) => ['keeper', 'conflicts', domain, id] as const,
  revision: (domain: DomainId, id: RevisionId) => ['keeper', 'revision', domain, id] as const,
  documents: (domain: DomainId) => ['keeper', 'documents', domain] as const,
  document: (domain: DomainId, id: string) => ['keeper', 'document', domain, id] as const,
  documentText: (domain: DomainId, id: string) => ['keeper', 'document-text', domain, id] as const,
  proposalItems: (domain: DomainId, id: string) =>
    ['keeper', 'proposal-items', domain, id] as const,
  sourceRuns: (domain: DomainId, params: SourceRunListParams | undefined) =>
    ['keeper', 'source-runs', domain, params ?? null] as const,
  apiKeys: (domain: DomainId) => ['keeper', 'api-keys', domain] as const,
  principals: (params: PrincipalListParams | undefined) =>
    ['keeper', 'principals', params ?? null] as const,
  grants: (domain: DomainId) => ['keeper', 'grants', domain] as const,
  audit: (domain: DomainId) => ['keeper', 'audit', domain] as const,
};

// ------------------------------------------------------- Domain and Branch

export function useDomains() {
  const api = useKeeperApi();
  return useQuery({ queryKey: keys.domains(), queryFn: () => api.domains.list() });
}

export function useDomain(domain: DomainId) {
  const api = useKeeperApi();
  return useQuery({ queryKey: keys.domain(domain), queryFn: () => api.domains.get(domain) });
}

export function useBranches(domain: DomainId) {
  const api = useKeeperApi();
  return useQuery({ queryKey: keys.branches(domain), queryFn: () => api.branches.list(domain) });
}

export function useBranch(domain: DomainId, branch: BranchName | undefined) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.branch(domain, branch ?? ''),
    queryFn: () => api.branches.get(domain, branch ?? ''),
    enabled: branch !== undefined,
  });
}

export function useRevisionLog(
  domain: DomainId,
  branch: BranchName,
  head: RevisionId | undefined,
  params?: PageParams,
) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: [...keys.log(domain, branch, head), params ?? null],
    queryFn: () => api.revisions.log(domain, branch, params),
  });
}

export function useRevision(domain: DomainId, id: RevisionId | null) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.revision(domain, id ?? ''),
    queryFn: () => api.revisions.get(domain, id ?? ''),
    enabled: id !== null,
  });
}

// ------------------------------------------------------ scoped reads

export function useSchema(scope: QueryScope) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.schema(scope),
    queryFn: () => api.schema.get(toReadScope(scope)),
  });
}

/** The schema at the head of any Branch, for screens that look at a Branch other than the current one. */
export function useBranchSchema(domain: DomainId, branch: BranchName) {
  const api = useKeeperApi();
  const info = useBranch(domain, branch);
  const head = info.data?.head;
  return useQuery({
    queryKey:
      head === undefined
        ? ['keeper', 'schema-pending', domain, branch]
        : keys.schema({ domain, branch, head }),
    queryFn: () => api.schema.get({ domain, branch }),
    enabled: head !== undefined,
  });
}

export function useRecords(scope: QueryScope, kind: string | undefined, params?: RecordListParams) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.records(scope, kind ?? '', params),
    queryFn: () => api.records.list(toReadScope(scope), kind ?? '', params),
    enabled: kind !== undefined,
  });
}

export function useRecord(scope: QueryScope, kind: string, key: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.record(scope, kind, key),
    queryFn: () => api.records.get(toReadScope(scope), kind, key),
  });
}

export function useRecordLinks(scope: QueryScope, kind: string, key: string, relation: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.links(scope, kind, key, relation),
    queryFn: () => api.records.links(toReadScope(scope), kind, key, relation),
  });
}

export function useLineage(scope: QueryScope, kind: string, key: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.lineage(scope, kind, key),
    queryFn: () => api.records.lineage(toReadScope(scope), kind, key),
  });
}

export function useHistory(scope: QueryScope, kind: string, key: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.history(scope, kind, key),
    queryFn: () => api.records.history(toReadScope(scope), kind, key),
  });
}

export function useQueryRun(scope: QueryScope, query: Query, enabled = true) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.query(scope, query),
    queryFn: () => api.query.run(toReadScope(scope), query),
    enabled,
  });
}

export function useSearch(scope: QueryScope, request: SearchRequest, enabled = true) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.search(scope, request),
    queryFn: () => api.search.run(toReadScope(scope), request),
    enabled,
  });
}

export function useNeighborhood(scope: QueryScope, request: NeighborhoodRequest, enabled = true) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.neighborhood(scope, request),
    queryFn: () => api.query.neighborhood(toReadScope(scope), request),
    enabled,
  });
}

// ------------------------------------------------------------ Review

export function useProposals(domain: DomainId, params?: ProposalListParams) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.proposals(domain, params),
    queryFn: () => api.proposals.list(domain, params),
  });
}

export function useProposal(domain: DomainId, id: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.proposal(domain, id),
    queryFn: () => api.proposals.get(domain, id),
  });
}

export function useProposalDiff(domain: DomainId, id: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.proposalDiff(domain, id),
    queryFn: () => api.proposals.diff(domain, id),
  });
}

export function useConflicts(domain: DomainId, id: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.conflicts(domain, id),
    queryFn: () => api.conflicts.list(domain, id),
  });
}

/** The first Record of each given Kind, used to seed a default Canvas view. */
export function useFirstRecordRefs(scope: QueryScope, kinds: string[]) {
  const api = useKeeperApi();
  return useQueries({
    queries: kinds.map((kind) => {
      const params: RecordListParams = { limit: 1 };
      return {
        queryKey: keys.records(scope, kind, params),
        queryFn: () => api.records.list(toReadScope(scope), kind, params),
      };
    }),
    combine: (results) => ({
      pending: results.some((r) => r.isPending),
      refs: results.flatMap((r) => r.data?.rows.map((row) => row.ref) ?? []),
    }),
  });
}

// ------------------------------------------------------- Sources and Admin

export function useSourceRuns(domain: DomainId, params?: SourceRunListParams) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.sourceRuns(domain, params),
    queryFn: () => api.sources.runs(domain, params),
    // Runs need a Domain-wide grant; a 403 or 404 does not change when asked again.
    retry: false,
  });
}

export function useApiKeys(domain: DomainId) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.apiKeys(domain),
    queryFn: () => api.admin.apiKeys.list(domain),
  });
}

export function usePrincipals(params?: PrincipalListParams) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.principals(params),
    queryFn: () => api.admin.principals.list(params),
  });
}

export function useGrants(domain: DomainId) {
  const api = useKeeperApi();
  return useQuery({ queryKey: keys.grants(domain), queryFn: () => api.admin.grants.list(domain) });
}

/** The audit log, newest first, one page more each time `fetchNextPage` is called. */
export function useAuditLog(domain: DomainId, pageSize: number) {
  const api = useKeeperApi();
  return useInfiniteQuery({
    queryKey: keys.audit(domain),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api.admin.audit.list(domain, { limit: pageSize, cursor: pageParam }),
    getNextPageParam: (last) => last.next_cursor,
  });
}

// ------------------------------------------------------ Documents and extraction

/** Whether the extraction job of a document is still on its way. */
export const jobIsActive = (detail: DocumentDetail | undefined): boolean =>
  detail?.extraction_jobs.some((job) => job.status === 'queued' || job.status === 'running') ??
  false;

/** The library, newest first, one page more each time `fetchNextPage` is called. */
export function useDocuments(domain: DomainId) {
  const api = useKeeperApi();
  const { pageSize } = useConfig();
  return useInfiniteQuery({
    queryKey: keys.documents(domain),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.documents.list(domain, { limit: pageSize, cursor: pageParam }),
    getNextPageParam: (last) => last.next_cursor,
    // The library needs a Domain-wide `document.read`; asking again does not change a 403.
    retry: false,
  });
}

/** A document with its jobs; asks again every `jobPollMs` while a job is queued or running. */
export function useDocument(domain: DomainId, id: string) {
  const api = useKeeperApi();
  const { documents } = useConfig();
  return useQuery({
    queryKey: keys.document(domain, id),
    queryFn: () => api.documents.get(domain, id),
    refetchInterval: (query) =>
      documents.jobPollMs > 0 && jobIsActive(query.state.data) ? documents.jobPollMs : false,
  });
}

/** The parsed text of a document, or `null` when this Keeper does not serve it. */
export function useDocumentText(domain: DomainId, id: string) {
  const api = useKeeperApi();
  return useQuery({
    queryKey: keys.documentText(domain, id),
    queryFn: () => api.documents.text(domain, id),
    staleTime: Infinity,
  });
}

/** Every extraction item of a Proposal (all pages), in proposal order. */
export function useProposalItems(domain: DomainId, proposal: string, enabled = true) {
  const api = useKeeperApi();
  const { extraction } = useConfig();
  return useQuery({
    queryKey: keys.proposalItems(domain, proposal),
    enabled,
    queryFn: async (): Promise<ExtractionItem[]> => {
      const items: ExtractionItem[] = [];
      let cursor: string | null = null;
      do {
        const page = await api.proposals.items(domain, proposal, {
          limit: extraction.itemsPageSize,
          cursor,
        });
        items.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor !== null);
      return items;
    },
  });
}
