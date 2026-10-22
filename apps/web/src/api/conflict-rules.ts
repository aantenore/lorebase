/**
 * Conflict rules of the merge engine (design 6.9), mirrored from
 * packages/core/src/lorebase_core/merge/conflicts.py: the choices each kind accepts,
 * which kinds are reported as groups and which a merge policy can resolve.
 */
import type { ConflictCode, ResolutionChoice } from './types';

const PICK: ResolutionChoice[] = ['ours', 'theirs', 'base', 'custom'];

export const CHOICES: Record<ConflictCode, ResolutionChoice[]> = {
  DV1: PICK,
  DV2: PICK,
  DV3: PICK,
  SC1: PICK,
  SC2: PICK,
  SC3: ['custom'],
  XS1: ['drop', 'restore'],
  XS2: ['drop', 'restore'],
  XS3: ['custom', 'drop', 'restore'],
  XS4: ['custom', 'relax'],
  XS5: ['custom', 'relax'],
  XS6: ['custom', 'relax'],
  XS7: ['custom', 'drop', 'relax'],
  XS8: ['custom', 'relax'],
  XS9: ['drop', 'restore'],
  XS10: ['custom', 'relax'],
  XS11: ['custom', 'restore'],
};

/** Kinds reported as one Conflict over many members, with a count and a sample of keys. */
export const GROUPED_KINDS: ReadonlySet<ConflictCode> = new Set<ConflictCode>([
  'XS1',
  'XS2',
  'XS3',
  'XS4',
  'XS5',
  'XS6',
  'XS7',
  'XS8',
  'XS9',
]);

/**
 * Kinds a merge policy can resolve without a person (`prefer` and `cascade_dangling`;
 * `authority` and `newest_wins` for DV1 and DV3).
 */
export const MACHINE_RESOLVABLE: ReadonlySet<ConflictCode> = new Set<ConflictCode>([
  'DV1',
  'DV2',
  'DV3',
  'SC1',
  'SC2',
  'XS9',
]);
