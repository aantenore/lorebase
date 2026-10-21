/**
 * Schema inference (docs/inference.md, `POST /domains/{d}/schema:infer`): a proposed schema
 * with the evidence behind each element and the issues to settle. Nothing here is committed.
 */

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

export interface SchemaProposalDraft {
  domain: string;
  status: 'proposed';
  /** The schema content keyed by element id. The app reads `yaml`, which says the same. */
  schema: Record<string, unknown>;
  /** The proposed schema as a schema document (design 5.13). */
  yaml: string;
  evidence: InferenceEvidence[];
  /** Capped by Keeper; `issue_count` is the real number. */
  issues: InferenceIssue[];
  issue_count: number;
  tables: InferenceTable[];
}

/** A Postgres source named by a secret reference an administrator allowlisted; never a URL. */
export interface PostgresSourceRef {
  dsn: string;
  schema?: string;
  tables?: string[];
}

/** CSV or TSV files (the file name is the table name), or a Postgres source reference. */
export type InferRequest =
  | { files: File[]; options?: Record<string, unknown> }
  | { postgres: PostgresSourceRef; options?: Record<string, unknown> };
