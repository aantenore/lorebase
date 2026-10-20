import type {
  Branch,
  BranchName,
  CreateBranchInput,
  CreateDomainInput,
  CreateProposalInput,
  DecideItemsInput,
  DiffParams,
  DiffResponse,
  Domain,
  DomainId,
  ExecuteOperationRequest,
  ExecuteOperationResponse,
  HistoryResponse,
  LineageResponse,
  LinksResponse,
  MergeAttempt,
  MergeOutcome,
  NeighborhoodRequest,
  NeighborhoodResponse,
  Page,
  PageParams,
  Proposal,
  ProposalDetail,
  ProposalItem,
  ProposalListParams,
  Query,
  QueryResponse,
  ReadScope,
  RecordListParams,
  RecordPage,
  RecordResponse,
  Revision,
  RevisionId,
  Resolution,
  ResolveManyInput,
  ReviewInput,
  ProposalReview,
  Schema,
  SchemaChangeRequest,
  SchemaChangeResult,
  SearchRequest,
  SearchResponse,
  TraverseRequest,
  TraverseResponse,
} from './types';

/**
 * The Keeper surface the web app uses (design 11.1). One interface, two
 * adapters: `http` talks to Keeper, `mock` serves an in-memory sample Domain.
 * Every read takes a `ReadScope` so Branch and Revision context is explicit.
 */
export interface KeeperApi {
  readonly domains: {
    list(): Promise<Page<Domain>>;
    create(input: CreateDomainInput): Promise<Domain>;
    get(domain: DomainId): Promise<Domain>;
  };

  readonly branches: {
    list(domain: DomainId): Promise<Page<Branch>>;
    create(domain: DomainId, input: CreateBranchInput): Promise<Branch>;
    get(domain: DomainId, branch: BranchName): Promise<Branch>;
    delete(domain: DomainId, branch: BranchName): Promise<void>;
  };

  readonly revisions: {
    log(domain: DomainId, branch: BranchName, params?: PageParams): Promise<Page<Revision>>;
    get(domain: DomainId, revision: RevisionId): Promise<Revision>;
    diff(domain: DomainId, params: DiffParams): Promise<DiffResponse>;
  };

  readonly schema: {
    get(scope: ReadScope): Promise<Schema>;
    applyChanges(
      domain: DomainId,
      branch: BranchName,
      request: SchemaChangeRequest,
    ): Promise<SchemaChangeResult>;
  };

  readonly records: {
    list(scope: ReadScope, kind: string, params?: RecordListParams): Promise<RecordPage>;
    get(scope: ReadScope, kind: string, key: string): Promise<RecordResponse>;
    links(
      scope: ReadScope,
      kind: string,
      key: string,
      relation: string,
      params?: PageParams,
    ): Promise<LinksResponse>;
    lineage(scope: ReadScope, kind: string, key: string): Promise<LineageResponse>;
    history(
      scope: ReadScope,
      kind: string,
      key: string,
      params?: PageParams,
    ): Promise<HistoryResponse>;
  };

  readonly query: {
    run(scope: ReadScope, query: Query): Promise<QueryResponse>;
    traverse(scope: ReadScope, request: TraverseRequest): Promise<TraverseResponse>;
    neighborhood(scope: ReadScope, request: NeighborhoodRequest): Promise<NeighborhoodResponse>;
  };

  readonly search: {
    run(scope: ReadScope, request: SearchRequest): Promise<SearchResponse>;
  };

  readonly operations: {
    execute(
      domain: DomainId,
      operation: string,
      request: ExecuteOperationRequest,
    ): Promise<ExecuteOperationResponse>;
  };

  readonly proposals: {
    list(domain: DomainId, params?: ProposalListParams): Promise<Page<Proposal>>;
    create(domain: DomainId, input: CreateProposalInput): Promise<Proposal>;
    get(domain: DomainId, proposal: string): Promise<ProposalDetail>;
    diff(domain: DomainId, proposal: string, params?: PageParams): Promise<DiffResponse>;
    review(domain: DomainId, proposal: string, input: ReviewInput): Promise<ProposalReview>;
    /**
     * Applies the stored Resolutions, re-runs the merge and its consistency checks, and
     * commits the merge Revision, or reports the Conflicts that remain.
     */
    merge(domain: DomainId, proposal: string): Promise<MergeOutcome>;
    close(domain: DomainId, proposal: string): Promise<Proposal>;
    items(domain: DomainId, proposal: string, params?: PageParams): Promise<Page<ProposalItem>>;
    decideItems(
      domain: DomainId,
      proposal: string,
      input: DecideItemsInput,
    ): Promise<Page<ProposalItem>>;
  };

  readonly conflicts: {
    /** The merge attempt of a Proposal: Conflicts, stored Resolutions, automatic Resolutions. */
    list(domain: DomainId, proposal: string): Promise<MergeAttempt>;
    /** Stores the Resolution of one Conflict (the Conflict id is its fingerprint). */
    resolve(domain: DomainId, proposal: string, resolution: Resolution): Promise<Resolution>;
    /** Stores several Resolutions at once; answers with the updated attempt. */
    resolveMany(domain: DomainId, proposal: string, input: ResolveManyInput): Promise<MergeAttempt>;
  };
}
