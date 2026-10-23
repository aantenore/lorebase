import { inference as inferenceMessages } from '@/messages/inference';

/** The scheme of a secret reference such as `secret://env/NAME`. */
const SECRET_SCHEME = 'secret://';

/** Why `value` cannot be sent as a Postgres source, or `undefined` when it can. */
export function secretReferenceError(value: string): string | undefined {
  const text = value.trim();
  if (!text.startsWith(SECRET_SCHEME) || text.length === SECRET_SCHEME.length) {
    return inferenceMessages.source.reference.invalid(SECRET_SCHEME);
  }
  return undefined;
}
