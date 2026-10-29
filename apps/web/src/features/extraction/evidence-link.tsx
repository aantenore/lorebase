import { Link } from '@tanstack/react-router';
import { useScope, useScopeSearch } from '@/app/scope';
import { extraction as extractionMessages } from '@/messages/extraction';

/** What links to a passage: an extraction item, or any value read from a document. */
export interface EvidenceSource {
  /** The extraction item the viewer highlights, when there is one. */
  id?: string | undefined;
  evidence: { document_id: string; start: number; end: number } | null;
}

/** Jump from an item (or another value read from a document) to its quote in the viewer. */
export function EvidenceLink({
  item,
  children = extractionMessages.evidence.view,
}: {
  item: EvidenceSource;
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
