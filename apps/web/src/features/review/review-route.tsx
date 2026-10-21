import { getRouteApi } from '@tanstack/react-router';
import { ReviewPage } from './review-page';

const route = getRouteApi('/$domain/review/');

/** The Review list bound to its URL state; loaded lazily by the route. */
export function ReviewRoute() {
  const { status } = route.useSearch();
  return <ReviewPage status={status ?? 'open'} />;
}
