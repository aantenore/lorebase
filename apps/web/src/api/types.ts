/**
 * Keeper REST types. Names follow docs/glossary.md and the shapes follow
 * docs/design.md sections 5 (model), 6 (versioning), 8 (queries) and 11 (REST).
 * Responses use api names for Kinds, Attributes and Relations (design 8.1);
 * schema elements also carry their stable ids (design 5.1).
 */

// ---------------------------------------------------------------- identity

export type DomainId = string;
export type BranchName = string;
export type RevisionId = string;
/** Stable, prefixed ULID: `knd_...`, `att_...`, `rel_...`, `trt_...`, ... */
export type ElementId = string;
/** External Record reference: `{kind_api_name}/{key}`, for example `Supplier/S-1001`. */
export type RecordRef = string;

// ----------------------------------------------------------------- generic

export interface Page<T> {
  items: T[];
  /** Opaque cursor; `null` when there is no further page. */
  next_cursor: string | null;
}

/** Read context shared by every read: Domain, Branch and optional Revision or tag. */
export interface ReadScope {
  domain: DomainId;
  /** Defaults to the Domain default Branch when omitted. */
  branch?: BranchName;
  /** Revision id or tag; overrides the Branch head when present. */
  at?: string;
}

export interface PageParams {
  limit?: number;
  cursor?: string | null;
}

/** RFC 9457 problem details. */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  /** Machine-readable code, when Keeper provides one. */
  code?: string;
}

// ------------------------------------------------------------------ Domain

export interface DomainSettings {
  search_language?: string;
  materialization?: Record<string, unknown>;
  extraction_defaults?: Record<string, unknown>;
  merge_policies?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Domain {
  /** Slug, immutable. */
  id: DomainId;
  display_name: string;
  description: string | null;
  default_branch: BranchName;
  settings: DomainSettings;
  created_at: string;
  created_by: string;
}

export interface CreateDomainInput {
  id: DomainId;
  display_name: string;
  description?: string;
  default_branch?: BranchName;
}

// ------------------------------------------------------------ Branch, Revision

export interface Branch {
  name: BranchName;
  head: RevisionId;
  /** `branch` is movable; `tag` is an immutable pointer. */
  kind: 'branch' | 'tag';
  protected: boolean;
  /** Revisions on the default Branch that this Branch does not contain yet. */
  behind_default: number;
  /** Revisions on this Branch that the default Branch does not contain yet. */
  ahead_default: number;
}

export interface CreateBranchInput {
  name: BranchName;
  /** Branch, tag or Revision to start from. Defaults to the default Branch. */
  from?: string;
}

export type RevisionOriginType =
  'operation' | 'source_sync' | 'extraction' | 'merge' | 'schema_change' | 'import';

export interface Revision {
  id: RevisionId;
  parents: RevisionId[];
  root: string;
  domain: DomainId;
  author: { principal: string; on_behalf_of: string | null };
  message: string;
  origin: { type: RevisionOriginType; ref: string | null };
  created_at: string;
  stats: { partitions_changed: number; changes: number };
}

// ------------------------------------------------------------------- Schema

export type ScalarType =
  | 'string'
  | 'text'
  | 'integer'
  | 'decimal'
  | 'float'
  | 'boolean'
  | 'date'
  | 'timestamp'
  | 'duration'
  | 'enum'
  | 'json'
  | 'geo_point'
  | 'uri';

export type AttributeType = ScalarType | `array<${ScalarType}>`;
export type Sensitivity = 'pii' | 'confidential' | 'restricted';
export type ElementStatus = 'draft' | 'active' | 'deprecated';
export type Cardinality = 'one_to_one' | 'one_to_many' | 'many_to_one' | 'many_to_many';
export type SearchWeight = 'A' | 'B' | 'C' | 'D';

export interface AttributeConstraints {
  min?: number;
  max?: number;
  min_length?: number;
  max_length?: number;
  /** RE2 syntax. */
  pattern?: string;
  enum_values?: string[];
}

export interface Attribute {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  type: AttributeType;
  required: boolean;
  unique: boolean;
  default: unknown;
  constraints: AttributeConstraints;
  sensitivity: Sensitivity[];
  indexed: boolean;
  /** Full-text weight, or `null` when not searchable. */
  searchable: SearchWeight | null;
  /** Set when the value is computed by a Derivation (read-only). */
  derivation_id: ElementId | null;
  /** Source that owns this Attribute's values, used by merge policies. */
  authority: ElementId | null;
}

export interface TraitImplementation {
  trait_id: ElementId;
  /** Trait Attribute id to the Kind's own Attribute id. */
  attribute_map: Record<ElementId, ElementId>;
}

export interface KindSearchSettings {
  text_attributes: { attribute_id: ElementId; weight: SearchWeight }[];
  embedding_template: string | null;
}

export interface Kind {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  /** Attribute ids; empty means a minted ULID key. */
  primary_key: ElementId[];
  title_attribute: ElementId;
  attributes: Record<ElementId, Attribute>;
  traits: TraitImplementation[];
  status: ElementStatus;
  search: KindSearchSettings;
  icon: string | null;
  /** Presentation hint for Explorer and Canvas. */
  color: string | null;
  operations_builtin: { create: boolean; update: boolean; delete: boolean };
}

export interface Relation {
  id: ElementId;
  /** Forward name, for example `supplied_by`. */
  api_name: string;
  /** Inverse name, for example `supplies`. */
  inverse_api_name: string;
  display_name: string;
  description: string;
  from_kind: ElementId;
  to_kind: ElementId;
  cardinality: Cardinality;
  required_from: boolean;
  /** Link Attributes. */
  attributes: Record<ElementId, Attribute>;
  on_delete: 'cascade' | 'restrict';
  backing: 'explicit' | 'foreign_key';
}

export interface TraitAttribute {
  id: ElementId;
  name: string;
  type: AttributeType;
  required: boolean;
}

export interface Trait {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  attributes: TraitAttribute[];
}

export interface OperationParameter {
  name: string;
  type: AttributeType | 'ref';
  required: boolean;
  description: string;
  kind_ref?: string;
  enum_values?: string[];
}

export interface OperationStep {
  /** `create_record`, `update_record`, `delete_record`, `create_link` or `delete_link`. */
  action: string;
  /** Step arguments; values are CEL expressions. */
  with: Record<string, unknown>;
}

export interface Operation {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  parameters: OperationParameter[];
  preconditions: { expression: string; message: string }[];
  steps: OperationStep[];
  /** `never`, `always` or a CEL expression. */
  review_policy: string;
  permissions: string[];
  side_effects: string[];
  rate_limit: string | null;
  status: ElementStatus;
  /** Set for built-in Operations generated per Kind or Relation. */
  builtin: boolean;
}

export type DerivationVariant = 'expression' | 'aggregate' | 'path';

export interface Derivation {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  variant: DerivationVariant;
  /** Attribute or Relation the Derivation computes. */
  target: { type: 'attribute' | 'relation'; kind_id: ElementId; api_name: string };
  /** Variant-specific definition (CEL expression, aggregate or path). */
  definition: Record<string, unknown>;
}

export type SourceType = 'postgres_table' | 'postgres_query' | 'csv' | 'parquet' | 'http_json';
export type SyncMode = 'snapshot' | 'incremental';
export type SourceMergePolicy = 'auto_when_clean' | 'proposal' | 'auto_with_authority';

export interface Source {
  id: ElementId;
  api_name: string;
  type: SourceType;
  /** Secret reference such as `secret://env/ERP_DSN`; never a literal. */
  connection: string;
  target_kind: ElementId;
  key_mapping: string[];
  attribute_mapping: Record<
    ElementId,
    { column?: string; expression?: string; transform?: string }
  >;
  relation_mapping: Record<ElementId, { column: string; target_kind: ElementId }>;
  scope_filter: string | null;
  sync_mode: SyncMode;
  /** Cron expression, or `null` for manual. */
  schedule: string | null;
  target_branch_policy: { branch_template: string; merge_policy: SourceMergePolicy };
}

export interface SearchCapabilities {
  modes: ('lexical' | 'hybrid' | 'semantic')[];
  embedding: { model: string; dims: number } | null;
}

export interface Schema {
  domain: DomainId;
  branch: BranchName;
  revision: RevisionId;
  kinds: Kind[];
  relations: Relation[];
  traits: Trait[];
  operations: Operation[];
  derivations: Derivation[];
  sources: Source[];
  search: SearchCapabilities;
}

export interface SchemaChange {
  /** Change command, for example `add_attribute` or `rename_kind`. */
  command: string;
  /** Stable id of the element the command targets, when it exists already. */
  target?: ElementId;
  arguments: Record<string, unknown>;
}

export interface SchemaChangeRequest {
  changes: SchemaChange[];
  dry_run?: boolean;
}

export interface SchemaChangeResult {
  valid: boolean;
  revision: RevisionId | null;
  violations: Violation[];
  /** Data migrations the changes imply, such as casts and defaults. */
  migrations: { description: string; affected_values: number; failing_values: number }[];
}

export interface Violation {
  code: string;
  message: string;
  ref?: RecordRef;
}

// ------------------------------------------------------- Records and Links

export type AttributeValue = unknown;

export interface RecordRow {
  ref: RecordRef;
  kind: string;
  key: string;
  title: string;
  attributes: Record<string, AttributeValue>;
  /** Derived values, never edited directly. */
  derived: Record<string, AttributeValue>;
}

export interface RecordPage {
  revision: RevisionId;
  rows: RecordRow[];
  next_cursor: string | null;
}

export interface RecordResponse {
  revision: RevisionId;
  record: RecordRow;
}

export interface LinkedRecord {
  ref: RecordRef;
  title: string;
  /** Link Attribute values. */
  attributes: Record<string, AttributeValue>;
}

export interface LinksResponse {
  revision: RevisionId;
  relation: string;
  direction: 'forward' | 'inverse';
  links: LinkedRecord[];
  next_cursor: string | null;
}

export type LineageOriginType =
  'operation' | 'source_sync' | 'extraction' | 'merge_resolution' | 'schema_migration' | 'import';

export interface LineageEntry {
  attribute: string;
  value: AttributeValue;
  origin_type: LineageOriginType;
  origin: Record<string, unknown>;
  actor: string;
  at: string;
  revision: RevisionId;
}

export interface LineageResponse {
  revision: RevisionId;
  lineage: LineageEntry[];
}

export interface HistoryEntry {
  revision: RevisionId;
  at: string;
  actor: string;
  message: string;
  changed_attributes: string[];
}

export interface HistoryResponse {
  entries: HistoryEntry[];
  next_cursor: string | null;
}

// ------------------------------------------------ Query, search, traversal

export type WhereOperator =
  | 'eq'
  | 'ne'
  | 'lt'
  | 'lte'
  | 'gt'
  | 'gte'
  | 'in'
  | 'between'
  | 'contains'
  | 'prefix'
  | 'exists'
  | 'match'
  | 'near';

export type Where =
  | { and: Where[] }
  | { or: Where[] }
  | { not: Where }
  | { attr: string; op: WhereOperator; value?: unknown }
  | { derived: string; op: WhereOperator; value?: unknown }
  | { via: string; where?: Where };

export interface OrderBy {
  attr: string;
  dir: 'asc' | 'desc';
}

export interface QueryInclude {
  select?: string[];
  limit?: number;
}

/** Query DSL (design 8.2). Target a Kind or a Trait. */
export interface Query {
  kind?: string;
  trait?: string;
  where?: Where;
  select?: string[];
  include?: Record<string, QueryInclude>;
  order_by?: OrderBy[];
  limit?: number;
  cursor?: string | null;
}

export type RecordListParams = Pick<Query, 'where' | 'order_by' | 'limit' | 'cursor'>;

export interface QueryResponse {
  revision: RevisionId;
  rows: RecordRow[];
  next_cursor: string | null;
}

export interface TraverseRequest {
  from: RecordRef;
  path: { relation: string; where?: Where; limit?: number }[];
  select?: string[];
}

export interface TraverseResponse {
  revision: RevisionId;
  records: RecordRow[];
  paths?: RecordRef[][];
}

export interface NeighborhoodRequest {
  seeds: RecordRef[];
  /** 1 to 3. */
  depth: number;
  relations?: string[];
  kinds?: string[];
  max_nodes?: number;
}

export interface GraphNode {
  ref: RecordRef;
  kind: string;
  title: string;
}

export interface GraphEdge {
  from: RecordRef;
  to: RecordRef;
  relation: string;
}

export interface NeighborhoodResponse {
  revision: RevisionId;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export type SearchMode = 'lexical' | 'hybrid' | 'semantic';

export interface SearchRequest {
  q: string;
  kinds?: string[];
  where?: Where;
  mode?: SearchMode;
  query_vector?: number[];
  limit?: number;
}

export interface SearchResult {
  ref: RecordRef;
  kind: string;
  title: string;
  snippet: string;
  score: number;
}

export interface SearchResponse {
  revision: RevisionId;
  results: SearchResult[];
  next_cursor?: string | null;
}

// -------------------------------------------------------------- Diff

export type ChangeType = 'added' | 'modified' | 'removed';

export interface AttributeChange {
  attribute: string;
  before: AttributeValue;
  after: AttributeValue;
}

export interface DiffChange {
  element: 'record' | 'link' | 'schema';
  change: ChangeType;
  /** Kind api name for Records, Relation api name for Links, element type for schema. */
  kind: string;
  ref: string;
  attributes?: AttributeChange[];
}

export interface DiffResponse {
  from: string;
  to: string;
  summary: Record<string, Record<ChangeType, number>>;
  changes: DiffChange[];
  next_cursor: string | null;
}

export interface DiffParams {
  from: string;
  to: string;
  kinds?: string[];
  limit?: number;
  cursor?: string | null;
}

// --------------------------------------------------------------- Operations

export interface ExecuteOperationRequest {
  parameters: Record<string, unknown>;
  branch?: BranchName;
  dry_run?: boolean;
  idempotency_key?: string;
}

export interface OperationEdit {
  action: string;
  ref?: RecordRef;
  values?: Record<string, AttributeValue>;
}

export interface ExecuteOperationResponse {
  status: 'committed' | 'dry_run' | 'proposed' | 'rejected';
  edits: OperationEdit[];
  violations: Violation[];
  revision?: RevisionId;
  proposal?: string;
}

// ---------------------------------------------------------------- Proposals

export type ProposalOrigin = 'manual' | 'source_sync' | 'extraction' | 'operation';
export type ProposalStatus = 'open' | 'merged' | 'closed';

export interface ProposalCheck {
  name: string;
  status: 'passed' | 'failed' | 'pending';
  message?: string;
}

export interface MergePolicy {
  name: string;
  /** For example `prefer: theirs` or `authority`. */
  rule: string;
}

export interface Proposal {
  id: string;
  title: string;
  description: string;
  source_ref: BranchName;
  target_ref: BranchName;
  origin: ProposalOrigin;
  status: ProposalStatus;
  author: string;
  created_at: string;
  merged_revision: RevisionId | null;
  checks: ProposalCheck[];
  policies: MergePolicy[];
}

export type ReviewVerdict = 'approve' | 'request_changes' | 'comment';

export interface ProposalReview {
  reviewer: string;
  verdict: ReviewVerdict;
  comment: string;
  at: string;
}

export interface ProposalDetail extends Proposal {
  reviews: ProposalReview[];
}

export interface CreateProposalInput {
  source_ref: BranchName;
  target_ref: BranchName;
  title: string;
  description?: string;
}

export interface ProposalListParams extends PageParams {
  status?: ProposalStatus;
  origin?: ProposalOrigin;
}

export interface ReviewInput {
  verdict: ReviewVerdict;
  comment?: string;
}

export interface ExtractionEvidence {
  document_id: string;
  page: number | null;
  span: [number, number];
  quote: string;
}

export type ItemDecision = 'pending' | 'accepted' | 'rejected';

export interface ProposalItem {
  item_id: string;
  partition: string;
  element_key: string;
  attribute_id: ElementId | null;
  evidence: ExtractionEvidence | null;
  confidence: number | null;
  match_status: 'matched' | 'new' | 'ambiguous' | null;
  decision: ItemDecision;
  decided_by: string | null;
  decided_at: string | null;
}

export interface DecideItemsInput {
  item_ids: string[];
  decision: Exclude<ItemDecision, 'pending'>;
}

// ----------------------------------------------------------------- Conflicts

export type ConflictCode =
  | 'DV1'
  | 'DV2'
  | 'DV3'
  | 'SC1'
  | 'SC2'
  | 'SC3'
  | 'XS1'
  | 'XS2'
  | 'XS3'
  | 'XS4'
  | 'XS5'
  | 'XS6'
  | 'XS7'
  | 'XS8'
  | 'XS9'
  | 'XS10'
  | 'XS11';

export type ResolutionChoice =
  | 'ours'
  | 'theirs'
  | 'base'
  | 'custom'
  | 'keep_deleted'
  | 'keep_modified'
  | 'drop'
  | 'restore'
  | 'relax';

export interface ConflictResolution {
  choice: ResolutionChoice;
  /** Custom value when `choice` is `custom`. */
  value?: AttributeValue;
}

export interface Conflict {
  id: string;
  code: ConflictCode;
  fingerprint: string;
  partition: string;
  element_key: string;
  attribute_id: ElementId | null;
  payload: { base: AttributeValue; ours: AttributeValue; theirs: AttributeValue };
  /** More than 1 for grouped Conflicts. */
  member_count: number;
  resolution: ConflictResolution | null;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface BulkResolutionInput {
  conflict_ids: string[];
  resolution: ConflictResolution;
}

export interface MergeResult {
  proposal: Proposal;
  revision: RevisionId;
}
