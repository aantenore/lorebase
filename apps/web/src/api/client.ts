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
  DiffParams,
  DiffResponse,
  Domain,
  DomainId,
  ExecuteOperationRequest,
  ExecuteOperationResponse,
  ExplainedRecord,
  HistoryParams,
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
  TraceSessionPage,
  TraceSessionParams,
  TraceTimeline,
  TraverseRequest,
  TraverseResponse,
  Webhook,
  WebhookCreated,
  WhoAmI,
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
import type {
  DecideItemsInput,
  DecideItemsResult,
  DocumentDetail,
  DocumentInfo,
  DocumentText,
  DocumentUploaded,
  ExtractionItem,
  ItemListParams,
  UploadDocumentInput,
} from './extraction-types';
import type {
  FilePreview,
  InferRequest,
  PreviewOptions,
  SchemaProposalDraft,
} from './inference-types';
import type {
  DecideSuggestionsInput,
  DecideSuggestionsResult,
  SchemaSuggestion,
  SuggestionListParams,
} from './suggestion-types';
import type {
  DerivationPreview,
  DerivationPreviewRequest,
  SourceExpressionPreview,
  SourceExpressionPreviewRequest,
} from './preview-types';
import type { SourceRun, SourceRunListParams, SourceSyncQueued } from './sources-types';
import type {
  CandidateListParams,
  ReconciliationCandidate,
  ReconciliationDecision,
  ResolveCandidateInput,
  ResolveCandidatesInput,
  ResolveCandidatesResult,
  SweepQueued,
} from './reconciliation-types';

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

  readonly derivations: {
    /**
     * Evaluates a Derivation of an edited schema document on sample Records of its Kind on the
     * Branch of the scope (`POST .../derivations:preview`), with Keeper's permissions and
     * masking. The document is checked like a schema change (a 422 `ValidationError` with the
     * issues when it fails); nothing is written.
     */
    preview(scope: ReadScope, request: DerivationPreviewRequest): Promise<DerivationPreview>;
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
      params?: HistoryParams,
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
    /**
     * Extraction items of a Proposal in proposal order (`GET .../items`), filtered by status,
     * Kind or Relation, type and confidence. Empty for a Proposal that has none.
     */
    items(
      domain: DomainId,
      proposal: string,
      params?: ItemListParams,
    ): Promise<Page<ExtractionItem>>;
    /**
     * Accepts, rejects or resets items, one by one with edits or all that match a filter
     * (`POST .../items:decide`). Keeper re-materializes the extraction Branch; a set of accepted
     * items the Branch cannot take is a 422 `ValidationError` and nothing changes.
     */
    decideItems(
      domain: DomainId,
      proposal: string,
      input: DecideItemsInput,
    ): Promise<DecideItemsResult>;
  };

  readonly documents: {
    /** Newest first (`GET .../documents`). */
    list(domain: DomainId, params?: PageParams): Promise<Page<DocumentInfo>>;
    /** The document with its extraction jobs, newest first. */
    get(domain: DomainId, document: string): Promise<DocumentDetail>;
    /**
     * Stores a document and queues its extraction (`POST .../documents`, multipart or a JSON
     * URL). The same bytes twice answer with the stored document and `duplicate: true`.
     */
    upload(domain: DomainId, input: UploadDocumentInput): Promise<DocumentUploaded>;
    /**
     * The parsed text the item offsets index (`GET .../documents/{id}/text`), or `null` while
     * it is not parsed; the viewer then shows the evidence passages.
     */
    text(domain: DomainId, document: string): Promise<DocumentText | null>;
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

  readonly trace: {
    /**
     * Agent sessions, newest activity first (`GET /trace/sessions`). Installation-level: not
     * scoped to the Domain on screen, though `domain_id` narrows it to one.
     */
    sessions(params?: TraceSessionParams): Promise<TraceSessionPage>;
    /** The steps of one session in order (`GET /trace/sessions/{id}`). */
    timeline(sessionId: string, params?: { actor?: string }): Promise<TraceTimeline>;
  };

  readonly webhooks: {
    list(domain: DomainId): Promise<Webhook[]>;
    /** The signing secret is in the answer once and cannot be read again. */
    create(domain: DomainId, input: CreateWebhookInput): Promise<WebhookCreated>;
    delete(domain: DomainId, webhook: string): Promise<void>;
  };

  readonly inference: {
    /**
     * Profiles CSV files or an allowlisted Postgres source and proposes a schema
     * (`POST .../schema:infer`). Nothing is committed; the caller applies the edited
     * proposal as a schema change set.
     */
    infer(domain: DomainId, request: InferRequest): Promise<SchemaProposalDraft>;
    /**
     * Reads the first rows of one data file on Keeper (`POST .../schema:preview`), for formats
     * the browser cannot read (Parquet). Same permission and size limits as `infer`; nothing
     * is stored.
     */
    preview(domain: DomainId, file: File, options?: PreviewOptions): Promise<FilePreview>;
  };

  readonly suggestions: {
    /**
     * Kinds, Attributes and Relations that document discovery proposes
     * (`GET .../schema/suggestions`), by status and type. Pending ones below Keeper's minimum
     * support are listed only with `all`.
     */
    list(domain: DomainId, params?: SuggestionListParams): Promise<Page<SchemaSuggestion>>;
    /**
     * Accepts, rejects or resets suggestions (`POST .../schema/suggestions:decide`). The
     * accepted ones of one request become one schema change set on a new Branch, proposed as a
     * Proposal. An invalid change set is a 422 `ValidationError` and nothing changes.
     */
    decide(domain: DomainId, input: DecideSuggestionsInput): Promise<DecideSuggestionsResult>;
  };

  readonly reconciliation: {
    /**
     * Candidates of a Branch, newest first (`GET .../reconciliation`), by type, status, Kind,
     * Attribute and source identity. Kind-narrowed grants see the candidates of their Kinds only.
     */
    list(domain: DomainId, params?: CandidateListParams): Promise<Page<ReconciliationCandidate>>;
    get(domain: DomainId, candidate: string): Promise<ReconciliationCandidate>;
    /**
     * Decides a candidate (`POST .../resolution`): a claim, the current value, a custom value, a
     * policy, or for duplicates a merge. The write takes the Operation path: on a protected
     * Branch it opens a Proposal (`proposed`) and the candidate stays open until it merges.
     */
    resolve(
      domain: DomainId,
      candidate: string,
      input: ResolveCandidateInput,
    ): Promise<ReconciliationDecision>;
    dismiss(domain: DomainId, candidate: string, reason: string): Promise<ReconciliationDecision>;
    /** Reopens a dismissed candidate. */
    reopen(domain: DomainId, candidate: string, reason: string): Promise<ReconciliationDecision>;
    /** One policy or a dismissal over listed or filtered open candidates, each decided on its own. */
    resolveMany(domain: DomainId, input: ResolveCandidatesInput): Promise<ResolveCandidatesResult>;
    /** Queues a detection sweep of a Branch (the default one when absent) for Scribe. */
    sweep(domain: DomainId, branch?: BranchName): Promise<SweepQueued>;
  };

  readonly sources: {
    /**
     * Runs of the Sources of a Domain, newest first (`GET .../sources/{source}/runs`). With
     * `source` the runs of that Source, paged; without it the first page of every Source of
     * the default branch's schema, merged and cut to `limit`, with no next cursor.
     */
    runs(domain: DomainId, params?: SourceRunListParams): Promise<Page<SourceRun>>;
    /** Queues a run of one Source (by api name) for Scribe (`POST .../sources/{source}/sync`). */
    sync(domain: DomainId, source: string): Promise<SourceSyncQueued>;
    /**
     * Evaluates the expression mappings of a Source of an edited schema document on sample rows
     * (`POST .../sources:preview`), as Scribe evaluates them. The rows are sent; no connection
     * is opened. An invalid document is a 422 `ValidationError`.
     */
    previewExpressions(
      scope: ReadScope,
      request: SourceExpressionPreviewRequest,
    ): Promise<SourceExpressionPreview>;
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
