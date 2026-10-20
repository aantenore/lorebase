import { CHOICES, MACHINE_RESOLVABLE } from '../conflict-rules';
import type { Conflict, ConflictCode, JsonValue } from '../types';

function hash32(text: string, seed: number): number {
  let hash = (0x811c9dc5 ^ seed) >>> 0;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

/** A stable 64 character hex digest; the mock's stand-in for the engine's sha256. */
export function digest(text: string): string {
  return Array.from({ length: 8 }, (_, i) =>
    hash32(text, Math.imul(i + 1, 0x9e3779b1))
      .toString(16)
      .padStart(8, '0'),
  ).join('');
}

const sideHash = (value: JsonValue): string | null =>
  value === null ? null : digest(JSON.stringify(value));

export interface ConflictSpec {
  kind: ConflictCode;
  partition: string;
  key?: string | null;
  attribute?: string | null;
  base?: JsonValue;
  ours?: JsonValue;
  theirs?: JsonValue;
  count?: number;
  sample?: string[];
  message: string;
  details?: Record<string, JsonValue>;
}

/**
 * A Conflict as the engine reports it: choices and policy flag by kind, fingerprint over
 * code, location, the hashes of the three sides and, for groups, the member keys.
 */
export function makeConflict(spec: ConflictSpec): Conflict {
  const base = spec.base ?? null;
  const ours = spec.ours ?? null;
  const theirs = spec.theirs ?? null;
  const sample = spec.sample ?? [];
  const key = spec.key ?? null;
  const attribute = spec.attribute ?? null;
  const hashes = [sideHash(base), sideHash(ours), sideHash(theirs)];
  const fingerprint = digest(
    JSON.stringify([spec.kind, spec.partition, key, attribute, ...hashes, [...sample].sort()]),
  );
  return {
    kind: spec.kind,
    fingerprint,
    partition: spec.partition,
    key,
    attribute,
    base,
    ours,
    theirs,
    base_hash: hashes[0] ?? null,
    ours_hash: hashes[1] ?? null,
    theirs_hash: hashes[2] ?? null,
    count: spec.count ?? Math.max(sample.length, 1),
    sample,
    message: spec.message,
    details: spec.details ?? {},
    choices: [...CHOICES[spec.kind]],
    machine_resolvable: MACHINE_RESOLVABLE.has(spec.kind),
  };
}
