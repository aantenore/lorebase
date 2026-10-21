import { useQuery } from '@tanstack/react-query';
import { useKeeperApi } from '@/api/provider';
import type { SchemaIssue } from '@/api/schema-rules';
import type { SchemaChange } from '@/api/types';
import { useScope } from '@/app/scope';
import type { SchemaDocument } from './document';

/**
 * Dry run of a change set on the current Branch: the data migrations it implies and the
 * violations the Branch reports. Runs once the edit is locally valid and has changes.
 * `document` is the whole edited schema, the form Keeper takes a change set in.
 */
export function useChangeCheck(
  changes: SchemaChange[],
  document: SchemaDocument,
  locallyValid: boolean,
) {
  const scope = useScope();
  const api = useKeeperApi();
  const check = useQuery({
    queryKey: ['keeper', scope.domain, 'schema-check', scope.branch, scope.head, changes],
    queryFn: () =>
      api.schema.applyChanges(scope.domain, scope.branch, { changes, document, dry_run: true }),
    enabled: locallyValid && changes.length > 0,
  });
  const remote: SchemaIssue[] =
    check.data?.violations.map((v) => ({ path: v.path ?? '', code: v.code, message: v.message })) ??
    [];
  let blocked: string | null = null;
  if (check.isError) blocked = check.error.message;
  else if (check.data?.valid === false) blocked = `${scope.branch} reports violations.`;
  return { check, remote, blocked };
}
