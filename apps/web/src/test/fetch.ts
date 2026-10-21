/** The URL a `fetch` stub was called with, whatever form the caller used. */
export function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input;
  return input instanceof URL ? input.href : input.url;
}
