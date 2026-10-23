import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { describeError, ValidationError } from '@/api';
import type {
  DecideItemsResult,
  ExtractionItem,
  ItemDecision,
  ItemEdits,
} from '@/api/extraction-types';
import { useKeeperApi } from '@/api/provider';
import { useBranchSchema, useProposalItems } from '@/api/queries';
import type { ProposalDetail } from '@/api/types';
import { useConfig } from '@/config/context';
import { extraction as extractionMessages } from '@/messages/extraction';
import { Button } from '@/ui/button';
import { Badge, EmptyState, QueryBoundary } from '@/ui/layout';
import { LearnMore } from '@/ui/learn-more';
import { useToast } from '@/ui/toast';
import { BulkBar, FilterBar } from './item-filters';
import { ItemDetail } from './item-detail';
import {
  countBy,
  editsFor,
  effective,
  isFiltering,
  NO_ITEM_FILTER,
  passes,
  percent,
  stage,
  stageEdits,
  tableOrder,
  targetsOf,
  toDecideInput,
  toFilter,
  type Draft,
  type ItemFilterState,
} from './item-state';
import { ItemTable } from './item-table';

const text = extractionMessages;

/** What the decisions did: the counts of the updated Branch. */
function Outcome({ result }: { result: DecideItemsResult }) {
  const { materialized } = result;
  const skipped = Object.entries(materialized.skipped);
  return (
    <section
      aria-label={text.outcome.label}
      className="flex flex-col gap-1 rounded-md border border-success p-3 text-sm"
    >
      <p role="status">
        {text.outcome.applied(
          materialized.applied,
          materialized.changes,
          skipped.length,
          materialized.revision === null ? null : materialized.revision.slice(0, 12),
        )}
      </p>
      {skipped.length > 0 && (
        <ul className="list-disc pl-5 text-warning">
          {skipped.map(([id, reason]) => (
            <li key={id}>
              {id}: {reason}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Why Keeper refused the decisions: the findings of a 422, or the message of any other error. */
function Refusal({ error, onSelect }: { error: unknown; onSelect: (item: string) => void }) {
  if (!(error instanceof ValidationError)) {
    return (
      <p role="alert" className="rounded-md border border-danger p-3 text-sm text-danger">
        {describeError(error)}
      </p>
    );
  }
  return (
    <section
      role="alert"
      aria-label={text.refusal.label}
      className="flex flex-col gap-2 rounded-md border border-danger p-3 text-sm text-danger"
    >
      <p>{text.refusal.nothingChanged(error.message)}</p>
      <ul className="list-disc pl-5">
        {error.issues.map((issue, index) => {
          const item = issue.path?.[0] === 'items' ? issue.path[1] : undefined;
          return (
            <li key={`${issue.code}-${index.toString()}`}>
              {issue.message}{' '}
              {typeof item === 'string' && (
                <button
                  type="button"
                  className="text-accent underline"
                  onClick={() => {
                    onSelect(item);
                  }}
                >
                  {text.refusal.showItem}
                </button>
              )}
            </li>
          );
        })}
        {error.fields.map((field, index) => (
          <li key={`${field.pointer}-${index.toString()}`}>
            {field.pointer}: {field.message}
          </li>
        ))}
      </ul>
    </section>
  );
}

interface BodyProps {
  domain: string;
  proposal: ProposalDetail;
  items: ExtractionItem[];
  selected: string | null;
  onSelect: (id: string) => void;
}

function ItemsBody({ domain, proposal, items, selected, onSelect }: BodyProps) {
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { extraction } = useConfig();
  const schema = useBranchSchema(domain, proposal.target_ref);
  const [draft, setDraft] = useState<Draft>(new Map());
  const [filter, setFilter] = useState<ItemFilterState>(NO_ITEM_FILTER);
  const [result, setResult] = useState<DecideItemsResult | null>(null);
  const readOnly = proposal.status !== 'open';
  const threshold = extraction.acceptThreshold;

  const shownItems = items.map((item) => effective(item, draft));
  const shown = shownItems.filter((item) => passes(item, toFilter(filter)));
  const order = tableOrder(shown);
  const current = order.find((item) => item.id === selected) ?? order[0];
  const stored = items.find((item) => item.id === current?.id);

  const apply = useMutation({
    mutationFn: () => api.proposals.decideItems(domain, proposal.id, toDecideInput(draft)),
    meta: { inlineError: true },
    onSuccess: async (decided) => {
      setResult(decided);
      setDraft(new Map());
      toast.notify(
        text.outcome.toast(decided.items.length, decided.materialized.changes, proposal.source_ref),
        'success',
      );
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
    },
  });

  const decide = (item: ExtractionItem, decision: ItemDecision) => {
    setResult(null);
    setDraft((current) =>
      stage(
        current,
        items.filter((i) => i.id === item.id),
        decision,
      ),
    );
  };
  const decideMany = (targets: readonly ExtractionItem[], decision: ItemDecision) => {
    setResult(null);
    const ids = new Set(targets.map((t) => t.id));
    setDraft((current) =>
      stage(
        current,
        items.filter((i) => ids.has(i.id)),
        decision,
      ),
    );
  };
  const edit = (item: ExtractionItem, wanted: ItemEdits) => {
    const raw = items.find((i) => i.id === item.id);
    if (raw === undefined) return;
    setResult(null);
    setDraft((current) =>
      stageEdits(current, raw, editsFor(raw, wanted, current.get(raw.id)?.edits)),
    );
  };

  const counts = countBy(shownItems);
  const total = shownItems.length;
  const decided = counts.accepted + counts.rejected;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <progress
            aria-label={text.summary.decidedLabel}
            max={total}
            value={decided}
            className="h-2 w-40"
          />
          <span>{text.summary.decided(decided, total)}</span>
          <Badge tone="success">{text.summary.accepted(counts.accepted)}</Badge>
          <Badge tone="warning">{text.summary.pending(counts.pending)}</Badge>
          <Badge>{text.summary.rejected(counts.rejected)}</Badge>
          <span className="text-muted">
            {text.summary.meanConfidence(
              percent(
                total === 0 ? null : items.reduce((sum, i) => sum + (i.confidence ?? 0), 0) / total,
              ),
            )}
          </span>
        </div>
        {!readOnly && (
          <div className="flex flex-wrap items-center gap-2">
            <span role="status" className="text-sm text-muted">
              {draft.size === 0 ? text.summary.nothingStaged : text.summary.staged(draft.size)}
            </span>
            <Button
              disabled={draft.size === 0 || apply.isPending}
              onClick={() => {
                setDraft(new Map());
                apply.reset();
              }}
            >
              {text.summary.discard}
            </Button>
            <Button
              variant="primary"
              disabled={draft.size === 0 || apply.isPending}
              onClick={() => {
                apply.mutate();
              }}
            >
              {text.summary.apply(draft.size)}
            </Button>
          </div>
        )}
      </div>

      {result !== null && <Outcome result={result} />}
      {apply.isError && <Refusal error={apply.error} onSelect={onSelect} />}

      <FilterBar targets={targetsOf(items)} filter={filter} onChange={setFilter} />
      {!readOnly && (
        <BulkBar
          shown={shown}
          defaultThreshold={threshold}
          disabled={apply.isPending}
          onStage={decideMany}
        />
      )}

      {shown.length === 0 ? (
        <EmptyState>{isFiltering(filter) ? text.noMatch : text.noneInProposal}</EmptyState>
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(20rem,26rem)]">
          <ItemTable
            items={shown}
            draft={draft}
            selected={current?.id ?? null}
            threshold={threshold}
            readOnly={readOnly}
            onSelect={onSelect}
            onDecide={decide}
          />
          {current !== undefined && stored !== undefined && (
            <ItemDetail
              item={current}
              stored={stored}
              schema={schema.data}
              threshold={threshold}
              readOnly={readOnly}
              onDecide={(decision) => {
                decide(current, decision);
              }}
              onEdit={(wanted) => {
                edit(current, wanted);
              }}
            />
          )}
        </div>
      )}
      {!readOnly && (
        <p className="text-xs text-muted">
          {text.keys} <LearnMore path={text.docs} />
        </p>
      )}
    </div>
  );
}

/**
 * Extraction items of a Proposal: a table grouped by
 * Records, values and Links with filters, confidence bars, evidence and its document, a
 * candidate picker for ambiguous matches, inline edits, accept and reject per item or in
 * bulk, and "Apply decisions", which sends the staged decisions to Keeper in one request.
 */
export function ItemsPanel({
  domain,
  proposal,
  selected,
  onSelect,
}: {
  domain: string;
  proposal: ProposalDetail;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const items = useProposalItems(domain, proposal.id);
  return (
    <QueryBoundary query={items}>
      {(data) =>
        data.length === 0 ? (
          <EmptyState>{text.noItems}</EmptyState>
        ) : (
          <ItemsBody
            key={proposal.id}
            domain={domain}
            proposal={proposal}
            items={data}
            selected={selected}
            onSelect={onSelect}
          />
        )
      }
    </QueryBoundary>
  );
}
