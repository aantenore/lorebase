import { toneOf } from '@/features/inference/confidence-badge';
import type { BadgeTone } from '@/ui/layout';
import { extraction as extractionMessages } from '@/messages/extraction';
import { cn } from '@/ui/cn';
import { percent } from './item-state';

const FILL: Record<BadgeTone, string> = {
  neutral: 'bg-muted',
  success: 'bg-success',
  danger: 'bg-danger',
  warning: 'bg-warning',
  accent: 'bg-accent',
};

/**
 * Confidence as a bar and a number. The tone follows the accept threshold: from it on the bar
 * is green, a little below amber, further below red (the same rule as the inference badges).
 */
export function ConfidenceBar({
  confidence,
  threshold,
  label = extractionMessages.confidence.label,
}: {
  confidence: number | null;
  threshold: number;
  label?: string;
}) {
  if (confidence === null)
    return <span className="text-xs text-muted">{extractionMessages.confidence.none}</span>;
  const tone = toneOf(confidence, threshold);
  return (
    <div className="flex items-center gap-2">
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={confidence}
        aria-valuetext={percent(confidence)}
        className="h-2 w-16 shrink-0 overflow-hidden rounded-full bg-surface-2"
      >
        <div
          className={cn('h-full rounded-full', FILL[tone])}
          style={{ width: `${(Math.min(1, confidence) * 100).toString()}%` }}
        />
      </div>
      <span className="w-9 text-xs tabular-nums">{percent(confidence)}</span>
    </div>
  );
}
