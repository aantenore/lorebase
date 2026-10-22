import { getRouteApi, useNavigate } from '@tanstack/react-router';
import { ReviewPage } from './review-page';

const route = getRouteApi('/$domain/review/');

/** The Review screen bound to its URL state; loaded lazily by the route. */
export function ReviewRoute() {
  const { status, tab } = route.useSearch();
  const navigate = useNavigate();
  return (
    <ReviewPage
      status={status ?? 'open'}
      tab={tab ?? 'proposals'}
      onTab={(next) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, tab: next === 'suggestions' ? next : undefined }),
          replace: true,
        });
      }}
    />
  );
}
