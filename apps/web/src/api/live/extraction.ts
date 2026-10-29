import type {
  DecideItemsInput,
  DecideItemsResult,
  DocumentDetail,
  DocumentInfo,
  ExtractionItem,
  ExtractionJob,
  ItemEvidence,
  ItemFilter,
  ItemMatch,
  MatchCandidate,
} from '../extraction-types';
import type { components } from '../generated/keeper';
import type { JsonValue } from '../types';

type S = components['schemas'];

/**
 * Mappers between Keeper's extraction routes (`documents.py`, `proposal_items.py`) and the
 * view models. The wire models are close to the view models; what is loosely typed there
 * (`evidence`, `match`, `edits` are free-form objects) is read defensively here.
 */

type Raw = Record<string, unknown>;

const isRaw = (value: unknown): value is Raw =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const num = (value: unknown): number | null => (typeof value === 'number' ? value : null);

function evidenceOf(raw: unknown): ItemEvidence | null {
  if (!isRaw(raw)) return null;
  const start = num(raw['start']);
  const end = num(raw['end']);
  if (start === null || end === null) return null;
  return {
    document_id: text(raw['document_id']) ?? '',
    chunk: num(raw['chunk']),
    start,
    end,
    quote: text(raw['quote']) ?? '',
  };
}

function candidateOf(raw: unknown): MatchCandidate | null {
  if (!isRaw(raw) || typeof raw['key'] !== 'string') return null;
  return {
    key: raw['key'],
    title: text(raw['title']) ?? raw['key'],
    score: num(raw['score']) ?? 0,
    method: text(raw['method']) ?? '',
  };
}

function strings(raw: unknown): string[] | undefined {
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : undefined;
}

function matchOf(raw: unknown): ItemMatch | null {
  if (!isRaw(raw) || typeof raw['status'] !== 'string') return null;
  const candidates = Array.isArray(raw['candidates'])
    ? raw['candidates'].map(candidateOf).filter((c): c is MatchCandidate => c !== null)
    : undefined;
  const missing = strings(raw['missing']);
  const missingLinks = strings(raw['missing_links']);
  return {
    status: raw['status'] as ItemMatch['status'],
    method: text(raw['method']),
    score: num(raw['score']),
    ...(candidates === undefined ? {} : { candidates }),
    ...(typeof raw['complete'] === 'boolean' ? { complete: raw['complete'] } : {}),
    ...(missing === undefined ? {} : { missing }),
    ...(missingLinks === undefined ? {} : { missing_links: missingLinks }),
  };
}

export function mapItem(raw: Raw): ExtractionItem {
  return {
    id: raw['id'] as string,
    type: raw['type'] as ExtractionItem['type'],
    seq: raw['seq'] as number,
    decision: raw['decision'] as ExtractionItem['decision'],
    confidence: num(raw['confidence']),
    tier: num(raw['tier']),
    model: text(raw['model']),
    kind: text(raw['kind']),
    relation: text(raw['relation']),
    key: text(raw['key']),
    attribute: text(raw['attribute']),
    value: (raw['value'] ?? null) as JsonValue,
    current: (raw['current'] ?? null) as JsonValue,
    from: text(raw['from']),
    to: text(raw['to']),
    values: isRaw(raw['values']) ? (raw['values'] as Record<string, JsonValue>) : {},
    evidence: evidenceOf(raw['evidence']),
    match: matchOf(raw['match']),
    edits: isRaw(raw['edits']) ? raw['edits'] : null,
    decided_by: text(raw['decided_by']),
    decided_at: text(raw['decided_at']),
    not_applied: text(raw['not_applied']),
  };
}

export function mapDecided(raw: Raw): DecideItemsResult {
  const items = Array.isArray(raw['items']) ? (raw['items'] as Raw[]) : [];
  return {
    proposal_id: raw['proposal_id'] as string,
    title: raw['title'] as string,
    checks: isRaw(raw['checks']) ? raw['checks'] : {},
    items: items.map(mapItem),
    materialized: raw['materialized'] as DecideItemsResult['materialized'],
  };
}

export const mapDocument = (raw: S['DocumentOut']): DocumentInfo => ({
  ...raw,
  text_sha256: raw.text_sha256 ?? null,
  created_at: raw.created_at ?? null,
  source_key: raw.source_key ?? null,
  replaces: raw.replaces ?? null,
  retired_at: raw.retired_at ?? null,
  retired_by: raw.retired_by ?? null,
  purged_at: raw.purged_at ?? null,
  purged_by: raw.purged_by ?? null,
});

const mapJob = (raw: S['ExtractionJobOut']): ExtractionJob => ({
  ...raw,
  branch: raw.branch ?? null,
  proposal_id: raw.proposal_id ?? null,
  created_at: raw.created_at ?? null,
  finished_at: raw.finished_at ?? null,
  error: raw.error ?? null,
});

export const mapDocumentDetail = (raw: S['DocumentDetailOut']): DocumentDetail => ({
  ...mapDocument(raw),
  extraction_jobs: raw.extraction_jobs.map(mapJob),
  open_stale_candidates: raw.open_stale_candidates,
  can_retire: raw.can_retire,
  can_purge: raw.can_purge,
});

/** The filters of Keeper's item list, one query parameter per value. */
export function itemQuery(filter: ItemFilter | undefined) {
  const list = (values: readonly string[] | undefined) =>
    values === undefined || values.length === 0 ? undefined : { repeat: values };
  return {
    status: list(filter?.status),
    kind: list(filter?.kind),
    type: list(filter?.type),
    min_confidence: filter?.min_confidence,
    max_confidence: filter?.max_confidence,
  };
}

/** The decide body: `where` is sent with its decision, only the members that are set. */
export function decideBody(input: DecideItemsInput) {
  const where = input.where;
  return {
    decisions: input.decisions ?? [],
    ...(where === undefined
      ? {}
      : {
          where: {
            ...(where.status === undefined ? {} : { status: where.status }),
            ...(where.kind === undefined ? {} : { kind: where.kind }),
            ...(where.type === undefined ? {} : { type: where.type }),
            ...(where.min_confidence === undefined ? {} : { min_confidence: where.min_confidence }),
            ...(where.max_confidence === undefined ? {} : { max_confidence: where.max_confidence }),
          },
        }),
    ...(input.decision === undefined ? {} : { decision: input.decision }),
  };
}
