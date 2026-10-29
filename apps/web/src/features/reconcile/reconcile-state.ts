import type { ReconciliationCandidate, ReconciliationClaim } from '@/api/reconciliation-types';
import type { Attribute, JsonValue, Kind } from '@/api/types';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import type { BadgeTone } from '@/ui/layout';

const text = reconcileMessages;

const isMap = (value: unknown): value is Record<string, JsonValue> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** `Kind key · attribute`, or `Kind key / key` for duplicates. */
export function candidateTitle(candidate: ReconciliationCandidate): string {
  const kind = candidate.kind ?? candidate.kind_id;
  const place = `${kind} ${candidate.keys.join(' / ')}`;
  return candidate.attribute === null ? place : `${place} · ${candidate.attribute}`;
}

/** A value as one line of text: strings as they are, anything else as JSON. */
export function showValue(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return text.claim.none;
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/**
 * Whether two values are the same value. Keeper writes integers and decimals as JSON strings,
 * so `"30"` and `30` compare equal; anything else compares as JSON.
 */
export function sameValue(a: JsonValue | undefined, b: JsonValue | undefined): boolean {
  if (a === undefined || b === undefined) return a === b;
  const scalar = (v: JsonValue) => typeof v === 'number' || typeof v === 'string';
  if (scalar(a) && scalar(b)) return String(a) === String(b);
  return JSON.stringify(a) === JSON.stringify(b);
}

/** The claim that holds the value on the Branch now, if one does. */
export function currentClaim(
  candidate: ReconciliationCandidate,
  current: JsonValue | undefined,
): ReconciliationClaim | undefined {
  if (current === undefined) return undefined;
  return candidate.claims.find((claim) => sameValue(claim.value, current));
}

/** `source:src_x` as "Source x" (the Source's api name when known), `document:d` as "Document d". */
export function sourceLabel(
  source: string | null,
  sourceNames: ReadonlyMap<string, string> = new Map(),
): string {
  if (source === null) return text.claim.unknownSource;
  const at = source.indexOf(':');
  if (at < 0) return source;
  const type = source.slice(0, at);
  const id = source.slice(at + 1);
  const label = text.claim.sources[type];
  if (label === undefined) return source;
  return label(type === 'source' ? (sourceNames.get(id) ?? id) : id);
}

/** The validity of a claim, or `null` when it has none. */
export function periodOf(claim: ReconciliationClaim): string | null {
  if (claim.valid_from === null && claim.valid_to === null) return null;
  return text.claim.periodValue(
    claim.valid_from ?? text.claim.open,
    claim.valid_to ?? text.claim.open,
  );
}

export interface SignalBadge {
  key: string;
  label: string;
  tone: BadgeTone;
  hint?: string | undefined;
}

const score = (value: JsonValue | undefined): string | null =>
  typeof value === 'number' ? value.toFixed(2) : null;

/** The duplicate rules a candidate can carry, with what they compared. */
const DUPLICATE_RULES: Readonly<Record<string, string>> = {
  normalized: text.signals.normalized,
  trigram: text.signals.trigram,
  semantic: text.signals.semantic,
  ambiguous: text.signals.ambiguous,
};

/** The signals of a candidate as badges, in a stable order. */
export function signalBadges(signals: Record<string, JsonValue>): SignalBadge[] {
  const out: SignalBadge[] = [];
  for (const [key, value] of Object.entries(signals).sort(([a], [b]) => a.localeCompare(b))) {
    const hint = text.signals.hints[key];
    if (key === 'unit') out.push({ key, label: text.signals.unit, tone: 'warning', hint });
    else if (key === 'negation') {
      out.push({ key, label: text.signals.negation, tone: 'danger', hint });
    } else if (key === 'equivalent') {
      out.push({ key, label: text.signals.equivalent, tone: 'neutral', hint });
    } else if (key === 'periods') {
      out.push({ key, label: text.signals.periods, tone: 'accent', hint });
    } else if (key === 'similarity') {
      out.push({
        key,
        label: text.signals.similarity(score(value) ?? showValue(value)),
        tone: 'neutral',
      });
    } else if (key === 'judge') {
      const label =
        isMap(value) && typeof value['label'] === 'string' ? value['label'] : showValue(value);
      const judged = isMap(value) ? score(value['score']) : null;
      const model = isMap(value) && typeof value['model'] === 'string' ? value['model'] : undefined;
      out.push({
        key,
        label: text.signals.judge(judged === null ? label : `${label} ${judged}`),
        tone: 'neutral',
        hint: model,
      });
    } else if (key === 'stale') {
      const reasons = Array.isArray(value) ? value.map(showValue).join(', ') : showValue(value);
      out.push({ key, label: text.signals.stale(reasons), tone: 'warning' });
    } else if (key in DUPLICATE_RULES) {
      const similarity = isMap(value) ? score(value['similarity']) : null;
      const rule = DUPLICATE_RULES[key] ?? key;
      out.push({
        key,
        label: similarity === null ? rule : `${rule} ${similarity}`,
        tone: 'accent',
      });
    } else {
      out.push({ key, label: text.signals.other(key), tone: 'neutral', hint });
    }
  }
  return out;
}

/**
 * Ranks loaded candidates as the design asks: a `negation` first, an `equivalent` last, the
 * rest in Keeper's order (newest first). Stable, so pages keep their order otherwise.
 */
export function rankCandidates(candidates: ReconciliationCandidate[]): ReconciliationCandidate[] {
  const weight = (c: ReconciliationCandidate) =>
    'negation' in c.signals ? 0 : 'equivalent' in c.signals ? 2 : 1;
  return candidates
    .map((candidate, index) => ({ candidate, index }))
    .sort((a, b) => weight(a.candidate) - weight(b.candidate) || a.index - b.index)
    .map((entry) => entry.candidate);
}

/** Signal names found on the loaded candidates, for the signal filter. */
export function signalNames(candidates: ReconciliationCandidate[]): string[] {
  return [...new Set(candidates.flatMap((c) => Object.keys(c.signals)))].sort();
}

/** `count` read up to the limit, "N+" when Keeper has more. */
export function countLabel(count: number, more: boolean): string {
  return more ? text.countMore(count) : count.toString();
}

/** Attributes whose values differ between two Records, by api name, in schema order. */
export function differingAttributes(
  attributes: readonly string[],
  first: Record<string, JsonValue>,
  second: Record<string, JsonValue>,
): string[] {
  return attributes.filter((name) => !sameValue(first[name], second[name]));
}

/** Whether reads may mask the Attribute's values: it is sensitive or held in the vault. */
export function maskable(attribute: Attribute): boolean {
  return attribute.sensitivity.length > 0 || attribute.vault === true;
}

/**
 * The stored Attributes of a duplicate merge whose values differ: those a person may pick, and
 * the maskable ones, which keep the value of the Record that stays (a read may show them
 * masked, and a masked value must never be written back).
 */
export function mergeAttributes(
  kind: Kind,
  first: Record<string, JsonValue>,
  second: Record<string, JsonValue>,
): { pickable: string[]; kept: string[] } {
  const stored = Object.values(kind.attributes).filter((a) => a.derivation_id === null);
  const differing = new Set(
    differingAttributes(
      stored.map((a) => a.api_name),
      first,
      second,
    ),
  );
  const pickable: string[] = [];
  const kept: string[] = [];
  for (const attribute of stored) {
    if (!differing.has(attribute.api_name)) continue;
    (maskable(attribute) ? kept : pickable).push(attribute.api_name);
  }
  return { pickable, kept };
}

/** The reason recorded with the last decision, if any. */
export function decisionReason(candidate: ReconciliationCandidate): string | null {
  const reason = candidate.decision?.['reason'];
  return typeof reason === 'string' && reason !== '' ? reason : null;
}
