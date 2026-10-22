/**
 * Schema inference (docs/inference.md, `POST /domains/{d}/schema:infer`): a proposed schema
 * with the evidence behind each element and the issues to settle. Nothing here is committed.
 */
import type { InferredSchema } from './types';

/** Why one element was proposed. `element` is a dotted path into the schema document. */
export interface InferenceEvidence {
  element: string;
  element_id: string;
  /** The table, or `table.column`, the element came from. */
  source: string;
  /** 0 to 1: the share of values supporting the decision. */
  confidence: number;
  reasons: string[];
  /** A few sample values, as text. */
  samples: string[];
}

export type InferenceSeverity = 'error' | 'warning';

/** An ambiguity or a gap the reviewer must settle, for example `infer.ambiguous_primary_key`. */
export interface InferenceIssue {
  code: string;
  severity: InferenceSeverity;
  message: string;
  element: string | null;
  source: string | null;
  details: Record<string, unknown>;
}

/** What one source table became. */
export interface InferenceTable {
  table: string;
  role: 'kind' | 'relation';
  element: string;
  rows: number;
  values_read: number;
}

/**
 * The generated `schema:infer` answer (`InferredSchema`) with its evidence, issues and tables
 * typed. `schema` is the content keyed by element id; the app reads `yaml` (the schema
 * document, design 5.13), which says the same. `issues` is capped by Keeper; `issue_count` is
 * the real number.
 */
export type SchemaProposalDraft = Omit<InferredSchema, 'evidence' | 'issues' | 'tables'> & {
  evidence: InferenceEvidence[];
  issues: InferenceIssue[];
  tables: InferenceTable[];
};

/** A Postgres source named by a secret reference an administrator allowlisted; never a URL. */
export interface PostgresSourceRef {
  dsn: string;
  schema?: string;
  tables?: string[];
}

/**
 * Data files (the file name is the table name; `recordsPath` locates the records array of the
 * JSON documents that hold it under a key), or a Postgres source reference.
 */
export type InferRequest =
  | { files: File[]; options?: Record<string, unknown>; recordsPath?: string }
  | { postgres: PostgresSourceRef; options?: Record<string, unknown> };

/**
 * The first rows of an uploaded file (`POST /domains/{d}/schema:preview`): the way to show a
 * format the browser cannot read, such as Parquet. Nothing is stored. Dates and decimals come
 * as text.
 */
export interface FilePreview {
  name: string;
  columns: string[];
  rows: Record<string, unknown>[];
  /** Rows in the file. */
  row_count: number;
  /** The file has more rows than were returned. */
  truncated: boolean;
}

export interface PreviewOptions {
  /** Rows to return; Keeper's default and cap apply otherwise. */
  rows?: number;
  /** Dotted path of the records array of a JSON document. */
  recordsPath?: string;
}
