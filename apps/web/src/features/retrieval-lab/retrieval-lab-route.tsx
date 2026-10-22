import { getRouteApi, useNavigate } from '@tanstack/react-router';
import { RetrievalLabPage } from './retrieval-lab-page';

const route = getRouteApi('/$domain/retrieval-lab');

/** The Retrieval lab bound to its URL state (`?q=<question>&budget=<tokens>`); loaded lazily. */
export function RetrievalLabRoute() {
  const { q, budget } = route.useSearch();
  const navigate = useNavigate();
  return (
    <RetrievalLabPage
      question={q}
      budget={budget}
      onAsk={(question, tokens) => {
        void navigate({
          to: '.',
          search: (previous) => ({ ...previous, q: question, budget: tokens }),
        });
      }}
    />
  );
}
