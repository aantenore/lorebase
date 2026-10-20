import { createFileRoute } from '@tanstack/react-router';
import { ProposalPage } from '@/features/review/proposal-page';

export const Route = createFileRoute('/$domain/review/$proposal')({
  component: ProposalRoute,
});

function ProposalRoute() {
  const { proposal } = Route.useParams();
  return <ProposalPage proposalId={proposal} />;
}
