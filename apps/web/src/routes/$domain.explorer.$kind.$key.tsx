import { createFileRoute } from '@tanstack/react-router';
import { RecordPage } from '@/features/explorer/record-page';

export const Route = createFileRoute('/$domain/explorer/$kind/$key')({
  component: RecordRoute,
});

function RecordRoute() {
  const { kind, key } = Route.useParams();
  return <RecordPage kind={kind} recordKey={key} />;
}
