/**
 * Keeper types. Names follow docs/glossary.md and the shapes follow docs/design.md sections
 * 5 (model), 6 (versioning), 8 (queries) and 11 (REST).
 *
 * Two layers live here. The wire layer is `./generated/keeper.ts`, produced from
 * docs/openapi.json (`pnpm types`); types that map one to one onto it are aliases of it
 * (the "contract" block below). The rest are the UI-facing view models the screens and both
 * adapters speak: the live adapter (`./live/`) maps Keeper responses onto them, the mock
 * builds them directly. Responses use api names for Kinds, Attributes and Relations
 * (design 8.1); schema elements also carry their stable ids (design 5.1).
 */
import type { components } from './generated/keeper';

type Schemas = components['schemas'];

// -------------------------------------------------------- contract (generated)

export type ProposalOrigin = Schemas['ProposalOrigin'];
export type ProposalStatus = Schemas['ProposalStatus'];
export type ReviewVerdict = Schemas['ReviewVerdict'];
export type ProposalCheck = Schemas['CheckOut'];
export type ProposalApproval = Schemas['ApprovalOut'];
export type ProposalReview = Schemas['ReviewOut'];
export type ReviewResult = Schemas['ReviewResult'];
export type Proposal = Schemas['lorebase_keeper__routes__proposals__ProposalOut'];
export type ProposalDetail = Schemas['ProposalDetailOut'];
export type CreateProposalInput = Schemas['ProposalCreate'];
export type ReviewInput = Schemas['ReviewIn'];
export type Domain = Schemas['DomainOut'];
export type CreateDomainInput = Schemas['DomainCreate'];
export type CreateBranchInput = Schemas['BranchCreate'];
export type WhereOperator = Schemas['Operator'];
export type WhoAmI = Schemas['WhoAmI'];
export type Webhook = Schemas['WebhookOut'];
export type WebhookCreated = Schemas['WebhookCreated'];
export type CreateWebhookInput = Schemas['CreateWebhook'];
export type ContextRequest = Partial<Schemas['ContextRequest']>;
export type ContextPack = Schemas['ContextPackOut'];
export type ExplainedRecord = Schemas['ExplainOut'];
export type SearchTuning = Schemas['SearchTuning'];
export type SearchCorrection = Schemas['SearchCorrection'];
/** One agent session as the trace lists it (`GET /trace/sessions`). */
export type TraceSession = Schemas['TraceSessionOut'];
export type TraceStep = Schemas['TraceStepOut'];
export type TraceRefs = Schemas['TraceRefs'];
export type TraceTimeline = Schemas['TraceTimeline'];
/** The schema Keeper proposes from uploaded tables (`POST .../schema:infer`). */
export type InferredSchema =
  components['schemas']['lorebase_keeper__routes__schema_inference__ProposalOut'];

// ---------------------------------------------------------------- identity

export type DomainId = string;
export type BranchName = string;
export type RevisionId = string;
/** Stable, prefixed ULID: `knd_...`, `att_...`, `rel_...`, `trt_...`, ... */
export type ElementId = string;
/** External Record reference: `{kind_api_name}/{key}`, for example `Asset/A-100`. */
export type RecordRef = string;

// ----------------------------------------------------------------- generic

/** Any JSON value; Conflict sides and Resolutions carry these. */
export type JsonValue =
  string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

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

/** RFC 9457 problem details as Keeper sends them (`application/problem+json`). */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  /** Machine-readable code, the last part of `type`. */
  code?: string;
  request_id?: string;
  /** Members of a rejected request body, query or path (422 `validation-failed`). */
  errors?: { pointer: string; message: string; code: string }[];
  /** Findings of a rejected schema change set or Operation (422); `path` lists names. */
  issues?: ProblemIssue[];
  /** The merge attempt carried by Keeper's `merge-conflicts` problem (409 of `.../merge`). */
  attempt?: MergeAttempt;
}

export interface ProblemIssue {
  code: string;
  message: string;
  severity?: string;
  path?: (string | number)[];
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

/** `decimal(12,2)` and `ref(Asset)` are the YAML spellings of precision and reference types. */
export type AttributeType =
  ScalarType | `array<${ScalarType}>` | `decimal(${number},${number})` | `ref(${string})`;
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
  /** Values are stored in the personal data vault (`pii` Attributes of a Kind only). */
  vault?: boolean;
  /** Values carry a validity period on their provenance (reconciliation, design 10.6). */
  valid_time?: boolean;
}

export interface TraitImplementation {
  trait_id: ElementId;
  /** Trait Attribute id to the Kind's own Attribute id. */
  attribute_map: Record<ElementId, ElementId>;
}

export interface KindSearchSettings {
  text_attributes: { attribute_id: ElementId; weight: SearchWeight }[];
  embedding_template: string | null;
  /** Lets the template name sensitive Attributes; `null` leaves the Keeper default (refuse). */
  embedding_sensitive: boolean | null;
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
  /** Forward name, for example `owned_by`. */
  api_name: string;
  /** Inverse name, for example `owns`. */
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
  /** Attribute of the `from` Kind that holds the target key, for `foreign_key` backing. */
  foreign_key_attribute?: ElementId | null;
  /** Keeper: omitted means `active`. */
  status?: ElementStatus;
}

export interface TraitAttribute {
  id: ElementId;
  name: string;
  type: AttributeType;
  required: boolean;
  /** Keeper: the document keeps these two when they are set. */
  display_name?: string;
  description?: string;
}

export interface Trait {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  attributes: TraitAttribute[];
  /** Keeper: omitted means `active`. */
  status?: ElementStatus;
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
  /** Set for built-in Operations provided per Kind or Relation. */
  builtin: boolean;
}

export type DerivationVariant = 'expression' | 'aggregate' | 'path';

export interface Derivation {
  id: ElementId;
  api_name: string;
  display_name: string;
  description: string;
  /** Keeper: omitted means `active`. */
  status?: ElementStatus;
  variant: DerivationVariant;
  /** Attribute or Relation the Derivation computes. */
  target: { type: 'attribute' | 'relation'; kind_id: ElementId; api_name: string };
  /**
   * Variant-specific part of the definition in the layout of the schema document, with api
   * names for references: `expression`, or `relation`, `direction`, `function`,
   * `source_attribute`, `filter` for an aggregate, or `steps` for a path.
   */
  definition: Record<string, unknown>;
}

/** Source types Keeper accepts, in the order the Source model lists them. */
export const SOURCE_TYPES = [
  'postgres_table',
  'postgres_query',
  'mysql_table',
  'mysql_query',
  'mssql_table',
  'mssql_query',
  'csv',
  'json',
  'jsonl',
  'parquet',
  'http_json',
] as const;
export type SourceType = (typeof SOURCE_TYPES)[number];
export type SyncMode = 'snapshot' | 'incremental';
export type SourceMergePolicy = 'auto_when_clean' | 'proposal' | 'auto_with_authority';
/** What an upstream change does to a value someone else set since the last sync. */
export type LocalEdits = 'review' | 'keep' | 'source_wins';

export interface SourceTransform {
  /** `cast`, `trim`, `lower`, `upper`, `parse_date`, `concat`, `constant`, `map` or `null_if`. */
  name: string;
  args?: Record<string, unknown>;
}

export interface Source {
  id: ElementId;
  api_name: string;
  /** Keeper: the document keeps these when they are set. */
  display_name?: string;
  description?: string;
  status?: ElementStatus;
  watermark_column?: string | null;
  tombstone_column?: string | null;
  type: SourceType;
  /** Secret reference such as `secret://env/ERP_DSN`; never a literal. */
  connection: string;
  /** `postgres_table`, `mysql_table`, `mssql_table`: the table, optionally schema-qualified. */
  table?: string;
  /** `postgres_query`, `mysql_query`, `mssql_query`: the read-only `SELECT`. */
  query?: string;
  /** `json` and `http_json`: dotted path of the array of records in the document. */
  records_path?: string;
  /** File and `http_json` sources: nested objects become `parent<separator>child` columns. */
  flatten_separator?: string;
  /** `http_json`: headers, pagination, `since_param`, `max_pages`; kept as Keeper sends it. */
  http?: Record<string, unknown>;
  target_kind: ElementId;
  key_mapping: string[];
  attribute_mapping: Record<
    ElementId,
    { column?: string; expression?: string; transforms?: SourceTransform[] }
  >;
  relation_mapping: { relation: ElementId; columns: string[] }[];
  scope_filter: string | null;
  sync_mode: SyncMode;
  /** Cron expression, or `null` for manual. */
  schedule: string | null;
  target_branch_policy: { branch_template?: string; merge_policy?: SourceMergePolicy };
  /** Absent means `review`. */
  local_edits?: LocalEdits;
  /** Keys the view does not model, kept verbatim so a save never drops them. */
  extra?: Record<string, unknown>;
}

export interface SearchCapabilities {
  modes: ('lexical' | 'hybrid' | 'semantic')[];
  embedding: { model: string; dims: number } | null;
}

/** A sample question about a Domain, declared in its schema (`domain.examples`). */
export interface DomainExample {
  label: string;
  question: string;
}

/** The `domain` block of a schema document, besides the id: kept so a save does not blank it. */
export interface SchemaDomainInfo {
  display_name?: string;
  description?: string;
  settings?: Record<string, unknown>;
  /** Sample questions offered as ready-made prompts; absent when the Domain declares none. */
  examples?: DomainExample[];
}

export interface Schema {
  domain: DomainId;
  /** Keeper: the `domain` block of the document. The mock leaves it out. */
  domain_info?: SchemaDomainInfo;
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

export type SchemaElementType =
  'kind' | 'attribute' | 'relation' | 'trait' | 'operation' | 'derivation' | 'source';

/** Change commands of a schema change set (design 11.1, `POST .../schema/changes`). */
export type SchemaCommand = `${'add' | 'update' | 'remove'}_${SchemaElementType}`;

/**
 * One command of a change set. `add_*` carries the new element in `arguments.element`
 * (`add_attribute` also names the owner in `arguments.owner`, a Kind or Relation id);
 * `update_*` names the element in `target` and the changed top-level fields in
 * `arguments.set`; `remove_*` only needs `target`.
 */
export interface SchemaChange {
  command: SchemaCommand;
  /** Stable id of the element the command targets, when it exists already. */
  target?: ElementId;
  arguments: Record<string, unknown>;
}

export interface SchemaChangeRequest {
  /** The commands the edit adds up to; the mock applies these and the Modeler lists them. */
  changes: SchemaChange[];
  /**
   * The whole schema as the document the changes lead to (the layout of
   * `lorebase_core.model.yaml_schema`). Keeper takes a change set in this form, so the live
   * adapter sends it and ignores `changes`; the mock ignores it.
   */
  document?: object;
  /** Revision message; Keeper writes a default when absent. */
  message?: string;
  dry_run?: boolean;
  /** Confirms that removing Kinds or Relations deletes their Records or Links. */
  cascade?: boolean;
}

/** A schema document checked against the schema of a Branch or Revision; nothing is written. */
export interface SchemaValidateRequest {
  document: object;
}

/** One schema element a change set creates, updates or deletes. */
export interface SchemaElementChange {
  op: 'create' | 'update' | 'delete';
  section: string;
  id: ElementId;
  api_name: string;
}

export interface SchemaValidation {
  valid: boolean;
  violations: Violation[];
  /** What the document would change on the schema it was checked against. */
  changes: SchemaElementChange[];
}

export interface SchemaMigration {
  description: string;
  affected_values: number;
  failing_values: number;
}

export interface SchemaChangeResult {
  valid: boolean;
  /** The new Revision; `null` for a dry run or when the change set is invalid. */
  revision: RevisionId | null;
  violations: Violation[];
  /** Data migrations the changes imply, such as casts and defaults. */
  migrations: SchemaMigration[];
  /** Keeper: the elements written or removed. */
  changes?: SchemaElementChange[];
  /** Keeper: Record and Link changes made by data migrations. */
  data_changes?: number;
}

export interface Violation {
  code: string;
  message: string;
  ref?: RecordRef;
  /** Schema path such as `kinds/Asset/attributes/status`, for schema violations. */
  path?: string;
  /** Keeper: `error` or `warning`. */
  severity?: string;
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

/** Where a value came from: `operation`, `source_sync`, `extraction`, `merge_resolution`, `schema_migration` or `import`. */
export type Provenance = components['schemas']['ProvenanceOut'];
export type AttributeLineage = components['schemas']['ValueLineageOut'];
export type ExtractionEvidence = components['schemas']['ExtractionEvidenceOut'];
export type LineageProposal = components['schemas']['LineageProposalOut'];
/** `GET .../records/{key}/lineage`: per-Attribute provenance of the current values. */
export type LineageResponse = components['schemas']['RecordLineageOut'];

export type AttributeValueChange = components['schemas']['AttributeChangeOut'];
export type RecordChange = components['schemas']['RecordChangeOut'];
/** `GET .../records/{key}/history`: the changes of one Record, newest first. */
export type HistoryResponse = components['schemas']['RecordHistoryOut'];

export interface HistoryParams extends PageParams {
  /** Add the values before and after each changed Attribute. */
  values?: boolean;
}

// ------------------------------------------------ Query, search, traversal

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
  /** Ranking knobs in place of the Domain's search settings; to compare parameter sets. */
  tuning?: SearchTuning;
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
  /** The mode that ran, which may differ from the one asked for. */
  mode?: string;
  /** Typo corrections applied to the query; hits found only through them come last. */
  corrections?: SearchCorrection[];
  next_cursor?: string | null;
}

// --------------------------------------------------------------- Trace

export interface TraceSessionParams extends PageParams {
  /** Only the sessions of this principal. */
  actor?: string;
  /** Only the sessions with a step in this Domain. */
  domain_id?: string;
}

/** A page of sessions; `enabled` is false while Keeper traces nothing (`keeper.trace.enabled`). */
export interface TraceSessionPage extends Page<TraceSession> {
  enabled: boolean;
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

export interface ProposalListParams extends PageParams {
  status?: ProposalStatus;
  origin?: ProposalOrigin;
}

// ----------------------------------------------------------------- Conflicts
// Mirrors packages/core/src/lorebase_core/merge/conflicts.py (design 6.8 to 6.10).

/** Stable Conflict codes: `DV` data, `SC` schema, `XS` cross-cutting. */
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

export type ResolutionChoice = 'ours' | 'theirs' | 'base' | 'custom' | 'drop' | 'restore' | 'relax';

/**
 * One Conflict of a merge attempt. `partition` and `key` locate the element (for schema
 * Conflicts the partition is `schema` and the key the element id); `attribute` is an
 * Attribute id for data Conflicts and a dotted property path for schema Conflicts.
 * `base`, `ours` and `theirs` hold the competing values (`null` means absent). Grouped
 * Conflicts carry `count` members and a `sample` of their keys.
 */
export interface Conflict {
  kind: ConflictCode;
  /** `sha256(code, partition, key, attribute, base_hash, ours_hash, theirs_hash)`; the Conflict id. */
  fingerprint: string;
  partition: string;
  key: string | null;
  attribute: string | null;
  base: JsonValue;
  ours: JsonValue;
  theirs: JsonValue;
  base_hash: string | null;
  ours_hash: string | null;
  theirs_hash: string | null;
  count: number;
  sample: string[];
  message: string;
  /**
   * Kind-specific facts. Conventions the UI reads: `path` (schema property path, or the
   * property a `relax` choice changes), `relaxed` (the value `relax` sets), `members`
   * (per-member `{base, ours, theirs}` of a grouped Conflict), `rejected` (why the last
   * merge run refused the stored Resolution).
   */
  details: Record<string, JsonValue>;
  /** Choices accepted for this Conflict (`CHOICES` of its kind). */
  choices: ResolutionChoice[];
  /**
   * A merge policy (`prefer`, `authority`, `newest_wins`, `cascade_dangling`) can resolve this
   * kind without a person.
   */
  machine_resolvable: boolean;
  /** Keeper: the Conflict id, equal to `fingerprint`. */
  id?: string;
  /** Keeper: `resolved` when the attempt applied a stored Resolution. */
  status?: 'open' | 'resolved';
  /** Keeper: every member key of a grouped Conflict (`sample` holds the first few). */
  members?: string[];
  /** Keeper: the Resolution the attempt applied. */
  resolution?: Resolution | null;
}

/** A resolver's decision for the Conflict with `fingerprint`. */
export interface Resolution {
  fingerprint: string;
  choice: ResolutionChoice;
  /** Custom value; `null` means absent. Only for `custom`. */
  value?: JsonValue;
  /** Custom values keyed by member key, for grouped Conflicts. Only for `custom`. */
  values?: Record<string, JsonValue>;
  /** Who decided; `policy:{name}` for policy resolutions. Set by Keeper. */
  resolver?: string;
  /** When it was stored. Set by Keeper. */
  at?: string;
  /** False when the Conflict it resolved is gone from the current attempt (a head moved). */
  matched?: boolean;
}

/** A Conflict resolved by the engine or by a policy, kept for Lineage and review. */
export interface AutoResolution {
  kind: ConflictCode;
  fingerprint: string;
  partition: string;
  key: string | null;
  attribute: string | null;
  choice: ResolutionChoice;
  resolver: string;
  count: number;
  sample: string[];
  /**
   * Why a Lineage-based policy decided: `ours_origin`/`theirs_origin` (`policy:authority`),
   * `ours_at`/`theirs_at` and `tie_break` (`policy:newest_wins`), and the provenance entries.
   */
  details?: Record<string, JsonValue>;
}

/**
 * The merge attempt stored on a Proposal when the merge found Conflicts (design 6.8 step 5).
 * Keeper's `GET .../conflicts` returns it with one page of `conflicts` (filters `kind`,
 * `status`, cursor `next_cursor`) and recomputes it when a head moved since; the members
 * marked Keeper are absent from the mock.
 */
export interface MergeAttempt {
  proposal: string;
  /**
   * Merge base, target head and source head the attempt was computed from. Keeper sends a
   * null base when the heads share no ancestor or the base is virtual (`virtual_base`).
   */
  base: RevisionId;
  ours: RevisionId;
  theirs: RevisionId;
  conflicts: Conflict[];
  /** Stored by fingerprint; a Conflict without an entry is unresolved. */
  resolutions: Resolution[];
  auto_resolutions: AutoResolution[];
  /** Keeper: attempt id. */
  id?: string;
  /** Keeper: `conflicted`, `resolved` (clean, not merged yet), `merged`, `stale`, `abandoned`. */
  status?: string;
  /** Keeper: `conflicts`, `merged`, `fast_forward` or `up_to_date`. */
  result?: string | null;
  virtual_base?: boolean;
  created_at?: string;
  created_by?: string | null;
  /** Keeper: target head after the merge committed. */
  revision?: RevisionId | null;
  policy?: Record<string, JsonValue>;
  open_conflicts?: number;
  resolved_conflicts?: number;
  /** Keeper: per Conflict code, open and resolved counts. */
  counts?: Record<string, { open: number; resolved: number }>;
  /** Keeper: the request ran the merge; `stale`: a head moved since the previous attempt. */
  recomputed?: boolean;
  stale?: boolean;
  /** Keeper: fingerprints of stored Resolutions whose Conflict is gone. */
  unmatched_resolutions?: string[];
  next_cursor?: string | null;
}

export interface ConflictListParams extends PageParams {
  kind?: ConflictCode[];
  status?: 'open' | 'resolved';
}

export interface ResolveManyInput {
  resolutions: Resolution[];
}

export interface MergeResult {
  proposal: Proposal;
  /** Head of the target branch after the merge. */
  revision: RevisionId;
  /** Keeper: `merged`, `fast_forward` or `up_to_date`. */
  result?: string;
  attempt?: MergeAttempt;
  /** Keeper: the source branch was deleted (`keeper.proposals.auto_close_source_branch`). */
  source_deleted?: boolean;
  replayed?: boolean;
}

/**
 * Result of `POST .../merge`: the merge Revision, or the attempt with the Conflicts that
 * remain after the stored Resolutions were applied and the consistency checks re-run.
 * Keeper answers the second case with HTTP 409 `merge-conflicts` carrying `attempt`; the
 * http adapter maps it to `{ status: 'conflicts', attempt }`.
 */
export type MergeOutcome =
  ({ status: 'merged' } & MergeResult) | { status: 'conflicts'; attempt: MergeAttempt };
