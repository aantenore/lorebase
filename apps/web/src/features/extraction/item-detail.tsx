import type { ExtractionItem, ItemDecision, ItemEdits } from '@/api/extraction-types';
import { useDocument } from '@/api/queries';
import type { Schema } from '@/api/types';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import { ConfidenceBar } from './confidence-bar';
import { EvidenceLink } from './evidence-link';
import { ItemEditor } from './item-editor';
import { describeItem, DECISION_TONE } from './item-table';
import { MatchBadge } from './match-badge';
import { MatchPicker } from './match-picker';
import { percent } from './item-state';

function Quote({ item }: { item: ExtractionItem }) {
  const scope = useScope();
  const evidence = item.evidence;
  const document = useDocument(scope.domain, evidence?.document_id ?? '');
  if (evidence === null) return <p className="text-sm text-muted">This item quotes no evidence.</p>;
  return (
    <figure className="flex flex-col gap-1">
      <blockquote className="border-l-2 border-accent pl-3 text-sm whitespace-pre-wrap">
        {evidence.quote}
      </blockquote>
      <figcaption className="text-xs text-muted">
        {document.data?.filename ?? evidence.document_id}, characters {evidence.start} to{' '}
        {evidence.end} <EvidenceLink item={item}>Open in document</EvidenceLink>
      </figcaption>
    </figure>
  );
}

function MatchDetails({ item }: { item: ExtractionItem }) {
  const match = item.match;
  if (match === null) return null;
  return (
    <div className="flex flex-col gap-1 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <MatchBadge item={item} />
        {match.method != null && <span className="text-muted">by {match.method}</span>}
        {match.score != null && <span className="text-muted">score {percent(match.score)}</span>}
      </p>
      {match.status === 'new' && (match.missing?.length ?? 0) > 0 && (
        <p className="text-warning">Missing values: {match.missing?.join(', ')}.</p>
      )}
      {match.status === 'new' && (match.missing_links?.length ?? 0) > 0 && (
        <p className="text-warning">
          Needs a {match.missing_links?.join(', ')} Link: accept the Link with this Record, or
          Keeper refuses the set.
        </p>
      )}
    </div>
  );
}

/**
 * The selected item: what it proposes, its evidence with a jump to the document, the match
 * and (when ambiguous) a candidate picker, inline edits, and the decision buttons. Nothing is
 * sent from here; decisions and edits are staged until "Apply decisions".
 */
export function ItemDetail({
  item,
  stored,
  schema,
  threshold,
  readOnly,
  onDecide,
  onEdit,
}: {
  /** The item with staged decision and edits laid over. */
  item: ExtractionItem;
  /** The item as Keeper holds it. */
  stored: ExtractionItem;
  schema: Schema | undefined;
  threshold: number;
  readOnly: boolean;
  onDecide: (decision: ItemDecision) => void;
  onEdit: (edits: ItemEdits) => void;
}) {
  const pick = (key: string) => {
    onEdit(item.type === 'link' ? { to: key } : { key });
  };
  return (
    <section
      aria-label={`Item ${describeItem(item)}`}
      className="flex flex-col gap-4 rounded-md border border-line bg-surface p-4"
    >
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="accent">{item.type}</Badge>
          <Badge tone={DECISION_TONE[item.decision]}>{item.decision}</Badge>
          {item.tier !== null && (
            <Badge>
              tier {item.tier}
              {item.model === null ? '' : `, ${item.model}`}
            </Badge>
          )}
        </div>
        <h3 className="text-sm font-semibold break-words">{describeItem(item)}</h3>
        <ConfidenceBar confidence={item.confidence} threshold={threshold} />
        {item.decided_by !== null && (
          <p className="text-xs text-muted">
            {item.decision} by {item.decided_by}
          </p>
        )}
      </header>

      {item.not_applied !== null && (
        <p role="alert" className="rounded-md border border-warning p-2 text-sm text-warning">
          Accepted, but not on the branch: {item.not_applied}
        </p>
      )}

      <Quote item={item} />
      <MatchDetails item={item} />
      <MatchPicker item={item} disabled={readOnly} onPick={pick} />

      {item.type === 'value' && (
        <p className="text-sm">
          <span className="text-muted">Current:</span>{' '}
          <code className="font-mono">{JSON.stringify(item.current)}</code>{' '}
          <span className="text-muted">Proposed:</span>{' '}
          <code className="font-mono">{JSON.stringify(item.value)}</code>
        </p>
      )}

      <ItemEditor
        key={`${stored.id}-${JSON.stringify(item.values)}-${JSON.stringify(item.value)}-${item.from ?? ''}-${item.to ?? ''}`}
        item={item}
        schema={schema}
        disabled={readOnly}
        onStage={onEdit}
      />

      {!readOnly && (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={item.decision === 'accepted'}
            onClick={() => {
              onDecide('accepted');
            }}
          >
            Accept
          </Button>
          <Button
            disabled={item.decision === 'rejected'}
            onClick={() => {
              onDecide('rejected');
            }}
          >
            Reject
          </Button>
          <Button
            variant="ghost"
            disabled={item.decision === 'pending'}
            onClick={() => {
              onDecide('pending');
            }}
          >
            Back to pending
          </Button>
        </div>
      )}
    </section>
  );
}
