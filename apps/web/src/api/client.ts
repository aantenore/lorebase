import type {
  Branch,
  BranchName,
  ConflictListParams,
  ContextPack,
  ContextRequest,
  CreateBranchInput,
  CreateDomainInput,
  CreateProposalInput,
  CreateWebhookInput,
  DecideItemsInput,
  DiffParams,
  DiffResponse,
  Domain,
  DomainId,
  ExecuteOperationRequest,
  ExecuteOperationResponse,
  ExplainedRecord,
  HistoryResponse,
  InferredSchema,
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
  ReviewResult,
  Schema,
  SchemaChangeRequest,
  SchemaChangeResult,
  SchemaValidateRequest,
  SchemaValidation,
  SearchRequest,
  SearchResponse,
  TraverseRequest,
  TraverseResponse,
  Webhook,
  WebhookCreated,
  WhoAmI,
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
    /** Checks a schema document against the schema of the scope; nothing is written. */
    validate(scope: ReadScope, request: SchemaValidateRequest): Promise<SchemaValidation>;
    /**
     * Commits a schema change set as one Revision, or dry-runs it. An invalid change set is
     * an answer (`valid: false` with the violations), not an error.
     */
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
    review(domain: DomainId, proposal: string, input: ReviewInput): Promise<ReviewResult>;
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

  readonly auth: {
    /** Who the credential of this connection belongs to. */
    whoami(): Promise<WhoAmI>;
  };

  readonly agents: {
    /** Seed Records, their neighborhood, schema, recent changes and lineage within a budget. */
    context(scope: ReadScope, request: ContextRequest): Promise<ContextPack>;
    /** Creation, per-Attribute lineage, Links with their origin and Proposals of a Record. */
    explain(scope: ReadScope, kind: string, key: string): Promise<ExplainedRecord>;
  };

  readonly inference: {
    /** Profiles CSV or TSV files and proposes a schema; nothing is committed. */
    upload(domain: DomainId, files: readonly File[]): Promise<InferredSchema>;
  };

  readonly webhooks: {
    list(domain: DomainId): Promise<Webhook[]>;
    /** The signing secret is in the answer once and cannot be read again. */
    create(domain: DomainId, input: CreateWebhookInput): Promise<WebhookCreated>;
    delete(domain: DomainId, webhook: string): Promise<void>;
  };
}
