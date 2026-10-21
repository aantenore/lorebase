import type { ExtractionItem, ItemDecision } from '@/api/extraction-types';

/** A stretch of the parsed text that an item quotes. */
export interface Span {
  item: string;
  start: number;
  end: number;
  decision: ItemDecision;
}

/** A piece of the text with the items whose evidence covers it (none: plain text). */
export interface Segment {
  start: number;
  end: number;
  items: string[];
  /** How to paint it: the most urgent decision among the items, `active` when one is selected. */
  tone: 'plain' | 'active' | ItemDecision;
}

const URGENCY: readonly ItemDecision[] = ['pending', 'accepted', 'rejected'];

/** Spans of the items whose evidence is in `document`, clipped to a text of `length`. */
export function spansOf(
  items: readonly ExtractionItem[],
  document: string,
  length: number,
): Span[] {
  return items.flatMap((item) => {
    const evidence = item.evidence;
    if (evidence?.document_id !== document) return [];
    const start = Math.max(0, Math.min(evidence.start, length));
    const end = Math.max(start, Math.min(evidence.end, length));
    return end > start ? [{ item: item.id, start, end, decision: item.decision }] : [];
  });
}

/**
 * Cuts the text at every span boundary. Overlapping spans share their segments, so a
 * sentence two items quote is one highlight that names both.
 */
export function segment(length: number, spans: readonly Span[], active: string | null): Segment[] {
  const cuts = [...new Set([0, length, ...spans.flatMap((s) => [s.start, s.end])])].sort(
    (a, b) => a - b,
  );
  const out: Segment[] = [];
  for (let index = 0; index + 1 < cuts.length; index += 1) {
    const start = cuts[index] ?? 0;
    const end = cuts[index + 1] ?? length;
    const covering = spans.filter((s) => s.start <= start && s.end >= end);
    const tone: Segment['tone'] = covering.some((s) => s.item === active)
      ? 'active'
      : (URGENCY.find((d) => covering.some((s) => s.decision === d)) ?? 'plain');
    out.push({ start, end, items: covering.map((s) => s.item), tone });
  }
  return out;
}
