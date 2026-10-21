/** Shown while a lazily loaded route (its code or its data) is not ready yet. */
export function RouteFallback() {
  return (
    <p role="status" aria-busy="true" className="p-4 text-muted">
      Loading screen...
    </p>
  );
}
