import { makeConflict } from '@/api/mock/conflicts';
import type { Conflict } from '@/api/types';
import {
  composeHunks,
  openHunks,
  pickText,
  textHunks,
  type Hunk,
  type HunkPick,
} from './text-hunks';

const hunks: Hunk[] = [
  { status: 'clean', base: 'A. ', ours: 'A. ', theirs: 'A! ', merged: 'A! ' },
  { status: 'conflict', base: 'B. ', ours: 'B1. ', theirs: 'B2. ', merged: null },
  { status: 'conflict', base: 'C.', ours: 'C1.', theirs: 'C2.', merged: null },
];

const conflictOf = (list: unknown, over: Partial<Conflict> = {}): Conflict => ({
  ...makeConflict({
    kind: 'DV1',
    partition: 'RiskFlag',
    key: 'F-1',
    attribute: 'att_reason',
    base: 'A. B. C.',
    ours: 'A. B1. C1.',
    theirs: 'A! B2. C2.',
    message: 'm',
  }),
  details: { text_hunks: list as never },
  ...over,
});

describe('textHunks', () => {
  it('reads the passages Keeper reports', () => {
    expect(textHunks(conflictOf(hunks))).toEqual(hunks);
  });

  it('gives nothing when absent, malformed or not adding up to the sides', () => {
    expect(textHunks({ ...conflictOf(hunks), details: {} })).toBeNull();
    expect(textHunks(conflictOf('nope'))).toBeNull();
    expect(textHunks(conflictOf([{ status: 'maybe' }]))).toBeNull();
    expect(textHunks(conflictOf([{ ...hunks[0], merged: null }]))).toBeNull();
    expect(textHunks(conflictOf([hunks[0]]))).toBeNull();
    expect(textHunks(conflictOf(hunks, { ours: 'something else' }))).toBeNull();
    expect(textHunks(conflictOf(hunks, { kind: 'DV3' }))).toBeNull();
  });
});

describe('composing passages', () => {
  const picks = (p: Record<number, HunkPick>) => p;

  it('builds each pick from the sides', () => {
    const conflict = hunks[2] as Hunk;
    expect(pickText(conflict, { kind: 'ours' })).toBe('C1.');
    expect(pickText(conflict, { kind: 'base' })).toBe('C.');
    expect(pickText(conflict, { kind: 'ours_theirs' })).toBe('C1. C2.');
    expect(pickText(conflict, { kind: 'theirs_ours' })).toBe('C2. C1.');
    expect(pickText(conflict, { kind: 'edit', text: 'own' })).toBe('own');
    // Trailing whitespace of the first part is kept, not doubled.
    const sentence = (ours: string, theirs: string): Hunk => ({
      status: 'conflict',
      base: '',
      ours,
      theirs,
      merged: null,
    });
    expect(pickText(sentence('a', 'b'), { kind: 'ours_theirs' })).toBe('a b');
    expect(pickText(sentence('a', ' b'), { kind: 'ours_theirs' })).toBe('a b');
    expect(pickText(sentence('a ', ' b'), { kind: 'ours_theirs' })).toBe('a b');
    expect(pickText(sentence('a\n', '  b'), { kind: 'theirs_ours' })).toBe('  b a\n');
    expect(pickText(sentence('a\n', '  b'), { kind: 'ours_theirs' })).toBe('a\nb');
    expect(pickText(hunks[1] as Hunk, { kind: 'ours_theirs' })).toBe('B1. B2. ');
  });

  it('is complete only when every conflicting passage has a pick', () => {
    expect(composeHunks(hunks, picks({ 1: { kind: 'ours' } }))).toBeNull();
    expect(openHunks(hunks, picks({ 1: { kind: 'ours' } }))).toBe(1);
    expect(composeHunks(hunks, picks({ 1: { kind: 'ours' }, 2: { kind: 'theirs_ours' } }))).toBe(
      'A! B1. C2. C1.',
    );
  });
});
