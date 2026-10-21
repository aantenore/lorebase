/** The scheme of a secret reference (design 5.9): `secret://env/ERP_DSN`, `secret://file/erp.dsn`. */
const SECRET_SCHEME = 'secret://';

/** Why `value` cannot be sent as a Postgres source, or `undefined` when it can. */
export function secretReferenceError(value: string): string | undefined {
  const text = value.trim();
  if (!text.startsWith(SECRET_SCHEME) || text.length === SECRET_SCHEME.length) {
    return `Use a secret reference such as ${SECRET_SCHEME}env/ERP_DSN; connection strings are never sent.`;
  }
  return undefined;
}
