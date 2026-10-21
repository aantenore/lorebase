/**
 * Documents and extraction items (docs/extraction.md, design 9.2 and 12.3).
 *
 * NOTE: typed by hand. The routes (`/documents`, `/proposals/{p}/items`, `items:decide`) are
 * not in `docs/openapi.json` on the default branch yet; once they are, replace these with
 * aliases of `./generated/keeper.ts` (`DocumentOut`, `DocumentDetailOut`, `DocumentUploaded`,
 * `ExtractionJobOut`, `ItemOut`, `DecideIn`, `DecideOut`). The shapes below follow the models
 * of the Keeper routes `documents.py` and `proposal_items.py`.
 */
import type { JsonValue, PageParams } from './types';

// ---------------------------------------------------------------- documents

export interface DocumentInfo {
  id: string;
  filename: string;
  media_type: string;
  /** Hex SHA-256 of the stored bytes. */
  sha256: string;
  size: number;
  /** Set once Scribe parsed the text. */
  text_sha256: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  created_by: string;
}

export type ExtractionJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | (string & {});

export interface ExtractionJob {
  id: string;
  status: ExtractionJobStatus;
  model: string;
  prompt_version: string;
  branch: string | null;
  proposal_id: string | null;
  created_at: string | null;
  finished_at: string | null;
  usage: Record<string, unknown>;
  stats: Record<string, unknown>;
  error: Record<string, unknown> | null;
}

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

export interface DocumentUploaded {
  document: DocumentInfo;
  /** The same bytes were stored before. */
  duplicate: boolean;
  job_id: number | null;
}

// -------------------------------------------------------------------- items

export type ItemType = 'record' | 'value' | 'link';
export type ItemDecision = 'pending' | 'accepted' | 'rejected';
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

export interface Materialized {
  revision: string | null;
  changes: number;
  applied: number;
  skipped: Record<string, string>;
}

export interface DecideItemsResult {
  proposal_id: string;
  title: string;
  /** `extraction` holds the item counts. */
  checks: { extraction?: ExtractionSummary } & Record<string, unknown>;
  /** The decided items. */
  items: ExtractionItem[];
  materialized: Materialized;
}
