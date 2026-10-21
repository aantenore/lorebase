/** `2026-10-19T02:04:11Z` as `2026-10-19 02:04 UTC`; anything else is returned as it is. */
export function shortTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return 'never';
  const found = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return found === null ? iso : `${found[1] ?? ''} ${found[2] ?? ''} UTC`;
}
