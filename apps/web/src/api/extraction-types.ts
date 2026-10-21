/**
 * Documents and extraction items (docs/extraction.md, design 9.2 and 12.3).
 *
 * The wire shapes are the generated ones of `./generated/keeper.ts`; the view models below
 * alias them, with the members Keeper may leave out made explicit (`null`) by the mappers in
 * `./live/extraction.ts`. Item evidence, match and edits are free-form objects on the wire,
 * so `ExtractionItem` and its parts stay typed here.
 */
import type { components } from './generated/keeper';
import type { JsonValue, PageParams } from './types';

type S = components['schemas'];

/** Members Keeper may leave out, made explicit. */
type Explicit<T, K extends keyof T> = Omit<T, K> & { [P in K]-?: Exclude<T[P], undefined> };

// ---------------------------------------------------------------- documents

export type DocumentInfo = Explicit<S['DocumentOut'], 'text_sha256' | 'created_at'>;

export type ExtractionJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | (string & {});

export type ExtractionJob = Omit<
  Explicit<S['ExtractionJobOut'], 'branch' | 'proposal_id' | 'created_at' | 'finished_at' | 'error'>,
  'status'
> & { status: ExtractionJobStatus };

/** A document with its extraction jobs, newest first. */
export interface DocumentDetail extends DocumentInfo {
  extraction_jobs: ExtractionJob[];
}

/** A multipart upload, or a document fetched by Keeper from an allowlisted URL. */
export type UploadDocumentInput = {
  /** Queue an extraction job; Keeper's default (`keeper.documents.extract_on_upload`) if absent. */
  extract?: boolean;
  /** Extraction tiers (0, 1, 2); Keeper's default if absent. */
  tiers?: number[];
} & ({ file: File } | { url: string; filename?: string });

export type DocumentUploaded = Omit<S['DocumentUploaded'], 'document' | 'job_id'> & {
  document: DocumentInfo;
  job_id: number | null;
};

/**
 * The parsed text the item offsets index (`GET .../documents/{id}/text`); offsets count
 * Unicode code points. `truncated`: Keeper cut it at `keeper.documents.max_text_chars`.
 */
export type DocumentText = Pick<S['DocumentTextOut'], 'text' | 'length' | 'truncated'>;

// -------------------------------------------------------------------- items

export type ItemType = S['ItemOut']['type'];
export type ItemDecision = S['ItemOut']['decision'];
export type MatchStatus = 'matched' | 'new' | 'ambiguous' | 'unresolved';

export interface ItemEvidence {
  document_id: string;
  chunk: number | null;
  /** Offsets into the parsed text of the document. */
  start: number;
  end: number;
  quote: string;
}

export interface MatchCandidate {
  key: string;
  title: string;
  score: number;
  method: string;
}

export interface ItemMatch {
  status: MatchStatus;
  method?: string | null;
  score?: number | null;
  candidates?: MatchCandidate[];
  /** New Records: every required Attribute has a value. */
  complete?: boolean;
  missing?: string[];
  /** New Records: required Links no item proposes. */
  missing_links?: string[];
}

/** Reviewer edits by item type (`record`: values; `value`: value, key; `link`: from, to, values). */
export interface ItemEdits {
  values?: Record<string, JsonValue>;
  value?: JsonValue;
  key?: string;
  from?: string;
  to?: string;
}

export interface ExtractionItem {
  id: string;
  type: ItemType;
  seq: number;
  decision: ItemDecision;
  confidence: number | null;
  tier: number | null;
  model: string | null;
  /** Kind api name (record and value items). */
  kind: string | null;
  /** Relation api name (link items). */
  relation: string | null;
  key: string | null;
  /** Attribute api name (value items). */
  attribute: string | null;
  value: JsonValue;
  /** The value on the target branch (value items). */
  current: JsonValue;
  from: string | null;
  to: string | null;
  /** Proposed values by Attribute api name, edits applied. */
  values: Record<string, JsonValue>;
  evidence: ItemEvidence | null;
  match: ItemMatch | null;
  edits: ItemEdits | null;
  decided_by: string | null;
  decided_at: string | null;
  /** Why an accepted item is not on the branch. */
  not_applied: string | null;
}

/** Filters of the item list and of a bulk decision; every given filter must match. */
export interface ItemFilter {
  status?: ItemDecision[];
  /** Kind or Relation api names. */
  kind?: string[];
  type?: ItemType[];
  /** Inclusive lower bound. */
  min_confidence?: number;
  /** Exclusive upper bound. */
  max_confidence?: number;
}

export interface ItemListParams extends PageParams, ItemFilter {}

export interface ItemDecisionInput {
  item: string;
  decision: ItemDecision;
  /** Omitted keeps the stored edits; `{}` clears them. */
  edits?: ItemEdits;
}

/** Decisions by item, and or one decision for every item matching `where`. */
export interface DecideItemsInput {
  decisions?: ItemDecisionInput[];
  where?: ItemFilter;
  decision?: ItemDecision;
}

export interface ExtractionSummary {
  items: number;
  decisions: Record<ItemDecision, number>;
  types: Record<string, number>;
  targets: Record<string, number>;
  mean_confidence: number | null;
  applied?: number;
  skipped?: Record<string, string>;
  revision?: string | null;
}

export type Materialized = S['MaterializedOut'];

export interface DecideItemsResult {
  proposal_id: string;
  title: string;
  /** `extraction` holds the item counts. */
  checks: { extraction?: ExtractionSummary } & Record<string, unknown>;
  /** The decided items. */
  items: ExtractionItem[];
  materialized: Materialized;
}
