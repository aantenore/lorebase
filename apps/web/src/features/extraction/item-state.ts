import type {
  DecideItemsInput,
  ExtractionItem,
  ExtractionSummary,
  ItemDecision,
  ItemEdits,
  ItemFilter,
  ItemType,
} from '@/api/extraction-types';
import type { JsonValue } from '@/api/types';
import { extraction as extractionMessages } from '@/messages/extraction';

/** A decision the reviewer has made but not sent yet; `edits` is the whole edit of the item. */
export interface Staged {
  decision: ItemDecision;
  edits?: ItemEdits;
}

export type Draft = ReadonlyMap<string, Staged>;

export const TYPE_ORDER: readonly ItemType[] = ['record', 'value', 'link'];

export const TYPE_LABEL: Record<ItemType, string> = {
  record: extractionMessages.types.record,
  value: extractionMessages.types.value,
  link: extractionMessages.types.link,
};

/** The Kind or Relation an item is about. */
export const targetOf = (item: ExtractionItem): string => item.relation ?? item.kind ?? '';

/** The item with the reviewer's edits (stored ones, or the staged ones that replace them) laid over. */
export function withEdits(
  item: ExtractionItem,
  edits: ItemEdits | null | undefined,
): ExtractionItem {
  if (edits === null || edits === undefined) return item;
  return {
    ...item,
    key: edits.key ?? item.key,
    value: edits.value ?? item.value,
    from: edits.from ?? item.from,
    to: edits.to ?? item.to,
    values: { ...item.values, ...edits.values },
  };
}

/** The item as the screen shows it: staged decision and edits over what Keeper holds. */
export function effective(item: ExtractionItem, draft: Draft): ExtractionItem {
  const staged = draft.get(item.id);
  if (staged === undefined) return item;
  // Keeper's `values` already hold the stored edits; the staged edits are whole, so they win.
  const base = staged.edits === undefined ? item : withEdits(item, staged.edits);
  return { ...base, decision: staged.decision };
}

export interface ItemFilterState {
  status: ItemDecision | 'all';
  /** Kind or Relation api name. */
  target: string;
  type: ItemType | 'all';
  /** Inclusive lower bound, as typed; empty is no bound. */
  min: string;
  /** Exclusive upper bound, as typed; empty is no bound. */
  max: string;
}

export const NO_ITEM_FILTER: ItemFilterState = {
  status: 'all',
  target: 'all',
  type: 'all',
  min: '',
  max: '',
};

/** A bound as typed: a number from 0 to 1.01, or `undefined` when empty or not a number. */
export function boundOf(text: string): number | undefined {
  if (text.trim() === '') return undefined;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 && value <= 1.01 ? value : undefined;
}

export function isFiltering(filter: ItemFilterState): boolean {
  return (
    filter.status !== 'all' ||
    filter.target !== 'all' ||
    filter.type !== 'all' ||
    boundOf(filter.min) !== undefined ||
    boundOf(filter.max) !== undefined
  );
}

/** Keeper's filter for the state, with the same meaning: every given member must match. */
export function toFilter(filter: ItemFilterState): ItemFilter {
  const min = boundOf(filter.min);
  const max = boundOf(filter.max);
  return {
    ...(filter.status === 'all' ? {} : { status: [filter.status] }),
    ...(filter.target === 'all' ? {} : { kind: [filter.target] }),
    ...(filter.type === 'all' ? {} : { type: [filter.type] }),
    ...(min === undefined ? {} : { min_confidence: min }),
    ...(max === undefined ? {} : { max_confidence: max }),
  };
}

/** Whether an item passes a filter; mirrors Keeper's item list (the upper bound is exclusive). */
export function passes(item: ExtractionItem, filter: ItemFilter): boolean {
  const { status, kind, type } = filter;
  if (status !== undefined && status.length > 0 && !status.includes(item.decision)) return false;
  if (type !== undefined && type.length > 0 && !type.includes(item.type)) return false;
  if (kind !== undefined && kind.length > 0 && !kind.includes(targetOf(item))) return false;
  const confidence = item.confidence ?? 0;
  if (filter.min_confidence !== undefined && confidence < filter.min_confidence) return false;
  if (filter.max_confidence !== undefined && confidence >= filter.max_confidence) return false;
  return true;
}

export interface ItemGroup {
  type: ItemType;
  items: ExtractionItem[];
}

/** Items grouped by type (Records, values, Links), in proposal order within a group. */
export function groupByType(items: readonly ExtractionItem[]): ItemGroup[] {
  return TYPE_ORDER.map((type) => ({
    type,
    items: items.filter((item) => item.type === type).sort((a, b) => a.seq - b.seq),
  })).filter((group) => group.items.length > 0);
}

/** The order the table shows and the keyboard walks. */
export const tableOrder = (items: readonly ExtractionItem[]): ExtractionItem[] =>
  groupByType(items).flatMap((group) => group.items);

/** The item `step` places after `from` in `order`, wrapping at both ends. */
export function neighbor(
  order: readonly ExtractionItem[],
  from: string | null,
  step: 1 | -1,
): ExtractionItem | undefined {
  if (order.length === 0) return undefined;
  const index = order.findIndex((item) => item.id === from);
  if (index < 0) return order[step === 1 ? 0 : order.length - 1];
  return order[(index + step + order.length) % order.length];
}

/** The Kinds and Relations the items are about, sorted, for the filter. */
export function targetsOf(items: readonly ExtractionItem[]): string[] {
  return [...new Set(items.map(targetOf).filter((name) => name !== ''))].sort();
}

export function countBy(items: readonly ExtractionItem[]): Record<ItemDecision, number> {
  const counts: Record<ItemDecision, number> = { pending: 0, accepted: 0, rejected: 0 };
  for (const item of items) counts[item.decision] += 1;
  return counts;
}

/** Counts of the items as Keeper's `extraction` check reports them, from a loaded list. */
export function summaryOf(items: readonly ExtractionItem[]): ExtractionSummary {
  const confidences = items.flatMap((i) => (i.confidence === null ? [] : [i.confidence]));
  const types: Record<string, number> = {};
  const targets: Record<string, number> = {};
  for (const item of items) {
    types[item.type] = (types[item.type] ?? 0) + 1;
    targets[targetOf(item)] = (targets[targetOf(item)] ?? 0) + 1;
  }
  return {
    items: items.length,
    decisions: countBy(items),
    types,
    targets,
    mean_confidence:
      confidences.length === 0 ? null : confidences.reduce((a, b) => a + b, 0) / confidences.length,
  };
}

/** The staged map with `decision` for each item, edits kept. */
export function stage(
  draft: Draft,
  items: readonly ExtractionItem[],
  decision: ItemDecision,
): Draft {
  const next = new Map(draft);
  for (const item of items) {
    const current = next.get(item.id);
    if (decision === item.decision && current?.edits === undefined) next.delete(item.id);
    else next.set(item.id, { ...current, decision });
  }
  return next;
}

/** The staged map with the edits of one item (and its decision unchanged unless staged). */
export function stageEdits(
  draft: Draft,
  item: ExtractionItem,
  edits: ItemEdits | undefined,
): Draft {
  const next = new Map(draft);
  const current = next.get(item.id);
  const decision = current?.decision ?? item.decision;
  if (edits === undefined && decision === item.decision) next.delete(item.id);
  else next.set(item.id, edits === undefined ? { decision } : { decision, edits });
  return next;
}

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Edits that differ from the item as Keeper holds it; `undefined` when nothing differs. */
export function editsFor(
  item: ExtractionItem,
  wanted: ItemEdits,
  /** The edits to build on: the staged ones, else those Keeper holds. */
  base: ItemEdits | undefined = item.edits ?? undefined,
): ItemEdits | undefined {
  const edits: ItemEdits = { ...(base ?? {}) };
  const set = <K extends keyof ItemEdits>(name: K, value: ItemEdits[K], current: unknown) => {
    if (value === undefined) return;
    if (sameJson(value, current)) delete edits[name];
    else edits[name] = value;
  };
  set('key', wanted.key, item.key);
  set('value', wanted.value, item.value);
  set('from', wanted.from, item.from);
  set('to', wanted.to, item.to);
  if (wanted.values !== undefined) {
    const values: Record<string, JsonValue> = { ...(base?.values ?? {}) };
    for (const [name, value] of Object.entries(wanted.values)) {
      if (sameJson(value, item.values[name] ?? null)) delete values[name];
      else values[name] = value;
    }
    if (Object.keys(values).length === 0) delete edits.values;
    else edits.values = values;
  }
  return Object.keys(edits).length === 0 ? undefined : edits;
}

/** The request that sends the staged decisions; an item is listed once, edits as staged. */
export function toDecideInput(draft: Draft): DecideItemsInput {
  return {
    decisions: [...draft].map(([item, staged]) => ({
      item,
      decision: staged.decision,
      ...(staged.edits === undefined ? {} : { edits: staged.edits }),
    })),
  };
}

export const percent = (value: number | null) =>
  value === null ? 'n/a' : `${Math.round(value * 100).toString()}%`;
