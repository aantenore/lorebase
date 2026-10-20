import { createFileRoute } from '@tanstack/react-router';
import { ReviewPage } from '@/features/review/review-page';
import { isStatusFilter, type StatusFilter } from '@/features/review/search';

export const Route = createFileRoute('/$domain/review/')({
  validateSearch: (raw: Record<string, unknown>): { status?: StatusFilter | undefined } => ({
    status: isStatusFilter(raw['status']) ? raw['status'] : undefined,
  }),
  component: ReviewRoute,
});

function ReviewRoute() {
  const { status } = Route.useSearch();
  return <ReviewPage status={status ?? 'open'} />;
}
