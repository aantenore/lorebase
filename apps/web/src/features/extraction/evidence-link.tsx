import { Link } from '@tanstack/react-router';
import type { ExtractionItem } from '@/api/extraction-types';
import { useScope, useScopeSearch } from '@/app/scope';

/** Jump from an item to its quote in the document viewer. */
export function EvidenceLink({
  item,
  children = 'View in document',
}: {
  item: ExtractionItem;
  children?: string;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  if (item.evidence === null) return null;
  return (
    <Link
      to="/$domain/documents/$document"
      params={{ domain: scope.domain, document: item.evidence.document_id }}
      search={{
        ...scopeSearch,
        item: item.id,
        start: item.evidence.start,
        end: item.evidence.end,
      }}
      className="text-accent hover:underline"
    >
      {children}
    </Link>
  );
}
