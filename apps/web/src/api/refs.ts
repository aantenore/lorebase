import type { RecordRef } from './types';

/** Build the external Record reference `{kind_api_name}/{key}`. */
export function refOf(kind: string, key: string): RecordRef {
  return `${kind}/${key}`;
}

/** Split a Record reference at the first slash; keys may themselves contain slashes. */
export function splitRecordRef(ref: RecordRef): { kind: string; key: string } {
  const index = ref.indexOf('/');
  return { kind: ref.slice(0, index), key: ref.slice(index + 1) };
}
