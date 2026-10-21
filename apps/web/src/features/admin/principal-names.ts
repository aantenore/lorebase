import { useMemo } from 'react';
import { usePrincipals } from '@/api/queries';

/** Display names of principals by id; an id with no known principal is shown as it is. */
export function usePrincipalNames(): (id: string) => string {
  const principals = usePrincipals();
  const names = useMemo(
    () => new Map((principals.data?.items ?? []).map((p) => [p.id, p.display_name])),
    [principals.data],
  );
  return (id) => names.get(id) ?? id;
}
