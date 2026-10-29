import { Link } from '@tanstack/react-router';
import { useMemo } from 'react';
import { useRecordCandidates } from '@/api/queries';
import type { ReconciliationCandidate } from '@/api/reconciliation-types';
import { useScope, useScopeSearch } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Badge } from '@/ui/layout';
import { DEFAULT_TYPE } from './search';

const text = reconcileMessages.record;

export interface RecordMarks {
  /** Open value candidates by Attribute api name. */
  byAttribute: ReadonlyMap<string, ReconciliationCandidate[]>;
  /** Open duplicate candidates the Record is part of. */
  duplicates: ReconciliationCandidate[];
}

/**
 * The open reconciliation candidates of one Record. Keeper lists by Kind; the first page of
 * open candidates of the Kind (`reconciliation.countLimit`) is matched on the key, so a Kind
 * with more open candidates than that may leave a value unmarked.
 */
export function useRecordMarks(kind: string, key: string): RecordMarks {
  const scope = useScope();
  const candidates = useRecordCandidates(scope.domain, scope.branch, kind, key);
  return useMemo(() => {
    const byAttribute = new Map<string, ReconciliationCandidate[]>();
    const duplicates: ReconciliationCandidate[] = [];
    for (const candidate of candidates.data ?? []) {
      if (candidate.type === 'duplicate') duplicates.push(candidate);
      else if (candidate.attribute !== null) {
        byAttribute.set(candidate.attribute, [
          ...(byAttribute.get(candidate.attribute) ?? []),
          candidate,
        ]);
      }
    }
    return { byAttribute, duplicates };
  }, [candidates.data]);
}

/** A badge that opens the (first) candidate in the Reconciliation screen. */
export function CandidateMark({
  candidates,
  label,
}: {
  candidates: ReconciliationCandidate[];
  label?: string;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const first = candidates[0];
  if (first === undefined) return null;
  return (
    <Link
      to="/$domain/reconcile"
      params={{ domain: scope.domain }}
      search={{
        ...scopeSearch,
        type: first.type === DEFAULT_TYPE ? undefined : first.type,
        candidate: first.id,
      }}
      title={text.open}
      className="inline-flex"
    >
      <Badge tone="warning">{label ?? text.badge(candidates.length)}</Badge>
    </Link>
  );
}
