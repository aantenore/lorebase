import type { Conflict } from '@/api/types';

/** One passage of a text value: kept as merged, or changed on both sides and waiting for a pick. */
export interface Hunk {
  status: 'clean' | 'conflict';
  base: string;
  ours: string;
  theirs: string;
  /** The merged text of a clean passage. */
  merged: string | null;
}

export type HunkPick =
  | { kind: 'ours' | 'theirs' | 'base' | 'ours_theirs' | 'theirs_ours' }
  | { kind: 'edit'; text: string };

export type PickKind = HunkPick['kind'];

const isText = (value: unknown): value is string => typeof value === 'string';

/**
 * The passages Keeper split a DV1 text value into (`details.text_hunks`), or `null` when
 * there are none, they are malformed, or they do not add up to the three sides.
 */
export function textHunks(conflict: Conflict): Hunk[] | null {
  const raw = conflict.details['text_hunks'];
  if (conflict.kind !== 'DV1' || !Array.isArray(raw) || raw.length === 0) return null;
  const hunks: Hunk[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return null;
    const { status, base, ours, theirs, merged } = item;
    if (status !== 'clean' && status !== 'conflict') return null;
    if (!isText(base) || !isText(ours) || !isText(theirs)) return null;
    if (merged !== null && !isText(merged)) return null;
    if (status === 'clean' && merged === null) return null;
    hunks.push({ status, base, ours, theirs, merged: merged ?? null });
  }
  if (!hunks.some((h) => h.status === 'conflict')) return null;
  const joined = (side: 'base' | 'ours' | 'theirs') => hunks.map((h) => h[side]).join('');
  const matches = (side: 'base' | 'ours' | 'theirs') => conflict[side] === joined(side);
  return matches('base') && matches('ours') && matches('theirs') ? hunks : null;
}

/**
 * `a` then `b` with exactly one break between them: a space when neither side has
 * whitespace at the junction, and the leading whitespace of `b` dropped when `a` ends in some.
 */
function both(a: string, b: string): string {
  if (a === '' || b === '') return a + b;
  const trailing = /\s$/.test(a);
  const leading = /^\s/.test(b);
  if (!trailing && !leading) return `${a} ${b}`;
  return trailing && leading ? a + b.replace(/^\s+/, '') : a + b;
}

export function pickText(hunk: Hunk, pick: HunkPick): string {
  switch (pick.kind) {
    case 'ours':
    case 'theirs':
    case 'base':
      return hunk[pick.kind];
    case 'ours_theirs':
      return both(hunk.ours, hunk.theirs);
    case 'theirs_ours':
      return both(hunk.theirs, hunk.ours);
    case 'edit':
      return pick.text;
  }
}

/** The text each passage contributes: merged for clean ones, the pick for the others (`null` while open). */
export function passageTexts(hunks: Hunk[], picks: Record<number, HunkPick>): (string | null)[] {
  return hunks.map((hunk, index) => {
    if (hunk.status === 'clean') return hunk.merged ?? '';
    const pick = picks[index];
    return pick === undefined ? null : pickText(hunk, pick);
  });
}

/** The whole text once every conflicting passage has a pick, `null` before. */
export function composeHunks(hunks: Hunk[], picks: Record<number, HunkPick>): string | null {
  const parts = passageTexts(hunks, picks);
  return parts.some((part) => part === null) ? null : parts.join('');
}

/** How many conflicting passages still have no pick. */
export function openHunks(hunks: Hunk[], picks: Record<number, HunkPick>): number {
  return hunks.filter((h, index) => h.status === 'conflict' && picks[index] === undefined).length;
}
