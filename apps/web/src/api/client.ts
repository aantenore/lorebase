import type {
  Branch,
  BranchName,
  ConflictListParams,
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
import type {
  ApiKey,
  ApiKeyCreated,
  AuditEntry,
  CreateApiKeyInput,
  Grant,
  Principal,
  PrincipalListParams,
} from './admin-types';
import type { InferRequest, SchemaProposalDraft } from './inference-types';
import type { SourceRun, SourceRunListParams } from './sources-types';

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
    /**
     * The merge attempt of a Proposal: Conflicts, stored Resolutions, automatic Resolutions.
     * Keeper pages the Conflicts (`params`) and recomputes the attempt when a head moved.
     */
    list(domain: DomainId, proposal: string, params?: ConflictListParams): Promise<MergeAttempt>;
    /** Stores the Resolution of one Conflict (the Conflict id is its fingerprint). */
    resolve(domain: DomainId, proposal: string, resolution: Resolution): Promise<Resolution>;
    /** Stores several Resolutions at once; answers with the updated attempt. */
    resolveMany(domain: DomainId, proposal: string, input: ResolveManyInput): Promise<MergeAttempt>;
  };

  readonly inference: {
    /**
     * Profiles CSV files or an allowlisted Postgres source and proposes a schema
     * (`POST .../schema:infer`). Nothing is committed; the caller applies the edited
     * proposal as a schema change set.
     */
    infer(domain: DomainId, request: InferRequest): Promise<SchemaProposalDraft>;
  };

  readonly sources: {
    /**
     * Runs of the Sources of a Domain, newest first. Keeper has no route for them yet:
     * the HTTP adapter fails with a 501 `not_supported` problem.
     */
    runs(domain: DomainId, params?: SourceRunListParams): Promise<Page<SourceRun>>;
    /** Starts a run of one Source (by api name). Same availability as `runs`. */
    sync(domain: DomainId, source: string): Promise<SourceRun>;
  };

  readonly admin: {
    apiKeys: {
      list(domain: DomainId, params?: PageParams): Promise<Page<ApiKey>>;
      create(domain: DomainId, input: CreateApiKeyInput): Promise<ApiKeyCreated>;
      revoke(domain: DomainId, keyId: string): Promise<void>;
    };
    /** Installation-wide: principals are not scoped to a Domain. */
    principals: { list(params?: PrincipalListParams): Promise<Page<Principal>> };
    grants: { list(domain: DomainId, params?: PageParams): Promise<Page<Grant>> };
    /** Keeper filters by nothing but paging; screens filter the loaded entries. */
    audit: { list(domain: DomainId, params?: PageParams): Promise<Page<AuditEntry>> };
  };
}
