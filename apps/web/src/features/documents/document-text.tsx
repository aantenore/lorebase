import { useEffect, useMemo, useRef } from 'react';
import { documents as documentsMessages } from '@/messages/documents';
import { cn } from '@/ui/cn';
import { segment, type Segment, type Span } from './document-spans';

const TONE: Record<Segment['tone'], string> = {
  plain: '',
  active: 'bg-accent/30 outline outline-2 outline-accent',
  pending: 'bg-warning/25',
  accepted: 'bg-success/25',
  rejected: 'bg-surface-2 text-muted line-through',
};

/**
 * The parsed text of a document with the evidence of the items highlighted: amber while a
 * decision is pending, green when accepted, struck through when rejected, outlined for the
 * selected item. Clicking a highlight selects the first item that quotes it.
 */
export function DocumentText({
  text,
  spans,
  active,
  onSelect,
}: {
  text: string;
  spans: readonly Span[];
  active: string | null;
  onSelect: (item: string) => void;
}) {
  const container = useRef<HTMLPreElement>(null);
  // Span offsets count code points (Keeper's offsets), so the text is cut by code point.
  const chars = useMemo(() => Array.from(text), [text]);
  const segments = segment(chars.length, spans, active);

  useEffect(() => {
    // jsdom has no layout and no scrollIntoView.
    container.current
      ?.querySelector('mark[data-tone="active"]')
      ?.scrollIntoView?.({ block: 'center' });
  }, [active, text]);

  return (
    <pre
      ref={container}
      aria-label={documentsMessages.viewer.textLabel}
      tabIndex={0}
      className="max-h-[70vh] overflow-auto rounded-md border border-line bg-surface p-3 font-mono text-sm whitespace-pre-wrap focus-visible:outline-2 focus-visible:outline-ring"
    >
      {segments.map((part) => {
        const piece = chars.slice(part.start, part.end).join('');
        if (part.items.length === 0) return piece;
        const first = part.items[0] ?? '';
        return (
          <mark
            key={part.start}
            data-start={part.start}
            data-items={part.items.join(' ')}
            data-tone={part.tone}
            className={cn('cursor-pointer rounded-sm px-0.5 text-ink', TONE[part.tone])}
            onClick={() => {
              onSelect(first);
            }}
          >
            {piece}
          </mark>
        );
      })}
    </pre>
  );
}
