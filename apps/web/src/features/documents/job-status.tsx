import type { ExtractionJob } from '@/api/extraction-types';
import { documents as documentsMessages } from '@/messages/documents';
import { Badge, type BadgeTone } from '@/ui/layout';

const TONE: Record<string, BadgeTone> = {
  queued: 'warning',
  running: 'accent',
  succeeded: 'success',
  failed: 'danger',
};

/** The sentence a failed job carries in its `error`, or a generic one. */
export function jobError(job: ExtractionJob): string {
  const message = job.error?.['message'];
  return typeof message === 'string' ? message : documentsMessages.jobs.failed;
}

/** Status of an extraction job as a badge; `none` for a document nobody queued a job for. */
export function JobBadge({ job }: { job: ExtractionJob | undefined }) {
  if (job === undefined) return <Badge>{documentsMessages.jobs.notExtracted}</Badge>;
  return (
    <span title={job.status === 'failed' ? jobError(job) : undefined}>
      <Badge tone={TONE[job.status] ?? 'neutral'}>{job.status}</Badge>
    </span>
  );
}

/** Tokens a job spent, as far as it reports them. */
export function tokensOf(job: ExtractionJob): number | null {
  const input = job.usage['input_tokens'];
  const output = job.usage['output_tokens'];
  if (typeof input !== 'number' && typeof output !== 'number') return null;
  return (typeof input === 'number' ? input : 0) + (typeof output === 'number' ? output : 0);
}

/** The cost estimate in a job's `usage`, as text, or `null` when it reports none. */
export function costOf(job: ExtractionJob): string | null {
  const cost = job.usage['cost'];
  return typeof cost === 'number' ? cost.toFixed(2) : null;
}
