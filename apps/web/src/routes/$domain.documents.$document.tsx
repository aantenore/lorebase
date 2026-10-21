import { createFileRoute } from '@tanstack/react-router';
import { readText } from '@/app/scope';
import { DocumentPage } from '@/features/documents/document-page';

/** A character offset in the URL: a whole number from 0, or absent. */
function offset(value: unknown): number | undefined {
  const text = readText(value);
  if (text === undefined) return undefined;
  const parsed = Number(text);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

export const Route = createFileRoute('/$domain/documents/$document')({
  validateSearch: (
    raw: Record<string, unknown>,
  ): { item?: string | undefined; start?: number | undefined; end?: number | undefined } => ({
    item: readText(raw['item']),
    start: offset(raw['start']),
    end: offset(raw['end']),
  }),
  component: DocumentRoute,
});

function DocumentRoute() {
  const { document } = Route.useParams();
  const { item, start, end } = Route.useSearch();
  return <DocumentPage documentId={document} item={item ?? null} start={start} end={end} />;
}
