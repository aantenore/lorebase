import { useId, type KeyboardEvent } from 'react';
import type { ExtractionItem, ItemDecision } from '@/api/extraction-types';
import { Button } from '@/ui/button';
import { extraction as extractionMessages } from '@/messages/extraction';
import { cn } from '@/ui/cn';
import { Badge, type BadgeTone } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { ConfidenceBar } from './confidence-bar';
import { EvidenceLink } from './evidence-link';
import { groupByType, neighbor, TYPE_LABEL, tableOrder, type Draft } from './item-state';
import { MatchBadge } from './match-badge';

const text = extractionMessages;

export const DECISION_TONE: Record<ItemDecision, BadgeTone> = {
  pending: 'warning',
  accepted: 'success',
  rejected: 'neutral',
};

/** What an item proposes, in a few words: the Record, the value change or the Link. */
export function describeItem(item: ExtractionItem): string {
  if (item.type === 'link') {
    return text.describe.link(item.relation, item.from, item.to);
  }
  if (item.type === 'value') {
    return text.describe.value(
      item.kind,
      item.key,
      item.attribute,
      JSON.stringify(item.current),
      JSON.stringify(item.value),
    );
  }
  return text.describe.record(item.kind, item.key);
}

export interface ItemTableProps {
  items: readonly ExtractionItem[];
  draft: Draft;
  selected: string | null;
  threshold: number;
  readOnly: boolean;
  onSelect: (id: string) => void;
  onDecide: (item: ExtractionItem, decision: ItemDecision) => void;
}

/**
 * The items grouped by type. The table is one grid with a roving selection: arrow keys and
 * `j` and `k` move between items, Home and End jump to the ends, `a` accepts, `r` rejects
 * and `u` puts the selected item back to pending (staged until "Apply decisions").
 */
export function ItemTable({
  items,
  draft,
  selected,
  threshold,
  readOnly,
  onSelect,
  onDecide,
}: ItemTableProps) {
  const base = useId();
  const order = tableOrder(items);
  const idOf = (item: ExtractionItem) => `${base}-${item.id}`;
  const active = order.find((item) => item.id === selected);

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.target instanceof HTMLElement && event.target.closest('a, input, select, textarea')) {
      return;
    }
    const key = event.key;
    let target: ExtractionItem | undefined;
    if (key === 'ArrowDown' || key === 'j') target = neighbor(order, selected, 1);
    else if (key === 'ArrowUp' || key === 'k') target = neighbor(order, selected, -1);
    else if (key === 'Home') target = order[0];
    else if (key === 'End') target = order[order.length - 1];
    else if (!readOnly && active !== undefined && (key === 'a' || key === 'r' || key === 'u')) {
      event.preventDefault();
      onDecide(active, key === 'a' ? 'accepted' : key === 'r' ? 'rejected' : 'pending');
      return;
    } else return;
    event.preventDefault();
    if (target !== undefined) onSelect(target.id);
  };

  return (
    <Table
      caption={text.table.caption}
      role="grid"
      tabIndex={0}
      aria-activedescendant={active === undefined ? undefined : idOf(active)}
      onKeyDown={onKeyDown}
      className="focus-visible:outline-2 focus-visible:outline-ring"
    >
      <THead>
        <TR>
          <TH>{text.table.columns.decision}</TH>
          <TH>{text.table.columns.change}</TH>
          <TH>{text.table.columns.confidence}</TH>
          <TH>{text.table.columns.match}</TH>
          <TH>{text.table.columns.evidence}</TH>
          <TH className="relative">
            <span className="sr-only">{text.table.columns.actions}</span>
          </TH>
        </TR>
      </THead>
      <TBody>
        {groupByType(items).flatMap((group) => [
          <TR key={`group-${group.type}`} className="bg-surface-2/60 hover:bg-surface-2/60">
            <TH colSpan={6} scope="colgroup" className="text-xs">
              {text.table.group(TYPE_LABEL[group.type], group.items.length)}
            </TH>
          </TR>,
          ...group.items.map((item) => {
            const staged = draft.has(item.id);
            const isSelected = item.id === selected;
            const label = describeItem(item);
            return (
              <TR
                key={item.id}
                id={idOf(item)}
                aria-selected={isSelected}
                onClick={() => {
                  onSelect(item.id);
                }}
                className={cn(
                  'cursor-pointer',
                  isSelected && 'bg-surface-2 outline -outline-offset-1 outline-accent',
                )}
              >
                <TD>
                  <Badge tone={DECISION_TONE[item.decision]}>{item.decision}</Badge>
                  {staged && <span className="ml-1 text-xs text-muted">{text.table.staged}</span>}
                </TD>
                <TD className="max-w-xs break-words">{label}</TD>
                <TD>
                  <ConfidenceBar confidence={item.confidence} threshold={threshold} />
                </TD>
                <TD>
                  <MatchBadge item={item} />
                </TD>
                <TD className="max-w-xs text-xs text-muted">
                  <span className="line-clamp-2 block">{item.evidence?.quote}</span>
                  <EvidenceLink item={item} />
                </TD>
                <TD>
                  {!readOnly && (
                    <span className="flex gap-1">
                      <Button
                        size="sm"
                        aria-label={text.table.accept(label)}
                        disabled={item.decision === 'accepted'}
                        onClick={(event) => {
                          event.stopPropagation();
                          onDecide(item, 'accepted');
                        }}
                      >
                        {text.accept}
                      </Button>
                      <Button
                        size="sm"
                        aria-label={text.table.reject(label)}
                        disabled={item.decision === 'rejected'}
                        onClick={(event) => {
                          event.stopPropagation();
                          onDecide(item, 'rejected');
                        }}
                      >
                        {text.reject}
                      </Button>
                    </span>
                  )}
                </TD>
              </TR>
            );
          }),
        ])}
      </TBody>
    </Table>
  );
}
