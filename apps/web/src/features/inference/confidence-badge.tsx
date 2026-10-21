import type { InferenceEvidence } from '@/api/inference-types';
import { Badge, type BadgeTone } from '@/ui/layout';

/** Distance below the threshold at which a confidence turns from caution to alarm. */
const CAUTION_BAND = 0.15;

export function toneOf(confidence: number, threshold: number): BadgeTone {
  if (confidence + 1e-9 >= threshold) return 'success';
  return confidence >= threshold - CAUTION_BAND ? 'warning' : 'danger';
}

export const percent = (value: number) => `${Math.round(value * 100).toString()}%`;

/** Confidence of an element as a badge; "no evidence" when the engine said nothing about it. */
export function ConfidenceBadge({
  evidence,
  threshold,
}: {
  evidence: InferenceEvidence | undefined;
  threshold: number;
}) {
  if (evidence === undefined) return <Badge>no evidence</Badge>;
  return (
    <Badge tone={toneOf(evidence.confidence, threshold)}>
      <span className="sr-only">Confidence </span>
      {percent(evidence.confidence)}
    </Badge>
  );
}

/** Reasons and sample values behind an element, behind a disclosure. */
export function EvidenceDetails({
  evidence,
  label = 'Why',
}: {
  evidence: InferenceEvidence | undefined;
  label?: string;
}) {
  if (evidence === undefined || evidence.reasons.length === 0) return null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-accent">{label}</summary>
      <ul className="mt-1 list-disc pl-4 text-muted">
        {evidence.reasons.map((reason, index) => (
          <li key={`${index.toString()}-${reason}`}>{reason}</li>
        ))}
      </ul>
      <p className="mt-1 text-muted">From {evidence.source}</p>
    </details>
  );
}
