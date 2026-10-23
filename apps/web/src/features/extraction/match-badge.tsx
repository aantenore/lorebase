import type { ExtractionItem } from '@/api/extraction-types';
import { extraction as extractionMessages } from '@/messages/extraction';
import { Badge, type BadgeTone } from '@/ui/layout';

const TONE: Record<string, BadgeTone> = {
  matched: 'success',
  new: 'accent',
  ambiguous: 'warning',
  unresolved: 'danger',
};

/** How the item's subject was resolved against the Records of the target Branch. */
export function MatchBadge({ item }: { item: ExtractionItem }) {
  const status = item.match?.status;
  if (status === undefined)
    return <span className="text-xs text-muted">{extractionMessages.match.notAvailable}</span>;
  const incomplete = status === 'new' && item.match?.complete === false;
  return (
    <Badge tone={incomplete ? 'warning' : (TONE[status] ?? 'neutral')}>
      {status}
      {incomplete && extractionMessages.match.incomplete}
    </Badge>
  );
}
