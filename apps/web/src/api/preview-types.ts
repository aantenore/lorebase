/** Previews of schema edits that are not saved yet (Derivations and Source expression mappings). */
import type { JsonValue } from './types';

/** Why a value could not be computed. `code` and `message` are null when the value is masked. */
export interface PreviewError {
  code: string | null;
  message: string | null;
}

/**
 * A Derivation of an edited schema document, evaluated on sample Records of its Kind on the
 * Branch of the scope (`POST .../derivations:preview`).
 */
export interface DerivationPreviewRequest {
  /** The schema document as edited; the Derivation is evaluated in the schema it produces. */
  document: object;
  /** Api name of the Derivation. */
  derivation: string;
  limit?: number;
}

export interface DerivationPreviewItem {
  key: string;
  title: string | null;
  /** The computed value; the masked value when `masked`. */
  value: JsonValue;
  /** Whether Keeper masked the value for this caller. */
  masked: boolean;
  error: PreviewError | null;
}

export interface DerivationPreview {
  revision: string;
  kind: string;
  attribute: string;
  items: DerivationPreviewItem[];
}

/**
 * The expression mappings of a Source in an edited schema document, evaluated on sample rows
 * (`POST .../sources:preview`). The rows are sent to Keeper; nothing is read from the Source.
 */
export interface SourceExpressionPreviewRequest {
  document: object;
  /** Api name of the Source. */
  source: string;
  rows: Record<string, JsonValue>[];
}

/** The value of one expression for one row, before the transforms of the mapping. */
export interface SourceExpressionCell {
  value: JsonValue;
  error: PreviewError | null;
}

export interface SourceExpressionPreview {
  /** Attributes mapped by an expression. */
  attributes: string[];
  /** One entry per row sent, in order. */
  rows: Record<string, SourceExpressionCell>[];
}
