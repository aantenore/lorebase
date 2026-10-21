import { getRouteApi } from '@tanstack/react-router';
import { DocumentPage } from './document-page';

const route = getRouteApi('/$domain/documents/$document');

/** One Document bound to its URL state; loaded lazily by the route. */
export function DocumentRoute() {
  const { document } = route.useParams();
  const { item, start, end } = route.useSearch();
  return <DocumentPage documentId={document} item={item ?? null} start={start} end={end} />;
}
