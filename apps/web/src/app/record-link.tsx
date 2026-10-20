import { Link } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { splitRecordRef } from '@/api/refs';
import type { RecordRef } from '@/api/types';
import { useScopeSearch } from './scope';

/** Link to a Record page that keeps the current Branch and Revision context. */
export function RecordLink({
  domain,
  recordRef,
  children,
}: {
  domain: string;
  recordRef: RecordRef;
  children?: ReactNode;
}) {
  const search = useScopeSearch();
  const { kind, key } = splitRecordRef(recordRef);
  return (
    <Link
      to="/$domain/explorer/$kind/$key"
      params={{ domain, kind, key }}
      search={search}
      className="text-accent underline-offset-2 hover:underline"
    >
      {children ?? recordRef}
    </Link>
  );
}
