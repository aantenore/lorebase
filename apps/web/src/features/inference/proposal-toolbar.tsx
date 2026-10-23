import type { InferenceTable } from '@/api/inference-types';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { useDraftView } from './draft-context';
import { inference as inferenceMessages } from '@/messages/inference';

export interface ProposalToolbarProps {
  /** Threshold as a whole percent, as typed. */
  percent: number;
  onPercent: (value: number) => void;
  tables: readonly InferenceTable[];
}

/** Bulk acceptance by confidence, the running counts and what the tables became. */
export function ProposalToolbar({ percent, onPercent, tables }: ProposalToolbarProps) {
  const { draft, threshold } = useDraftView();
  const { counts, analysis } = draft;
  const rows = tables.reduce((sum, t) => sum + t.rows, 0);
  const kinds = tables.filter((t) => t.role === 'kind').length;

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line bg-surface p-3">
      <p className="text-sm text-muted">
        {inferenceMessages.toolbar.summary(tables.length, rows, kinds)}
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-40">
          <Input
            label={inferenceMessages.toolbar.threshold}
            type="number"
            min={0}
            max={100}
            step={1}
            value={percent}
            onChange={(event) => {
              const value = Number(event.target.value);
              if (Number.isFinite(value)) onPercent(Math.min(100, Math.max(0, Math.round(value))));
            }}
          />
        </div>
        <Button
          variant="primary"
          onClick={() => {
            draft.acceptAtOrAbove(threshold);
          }}
        >
          {inferenceMessages.toolbar.acceptAll(percent)}
        </Button>
        <p role="status" aria-label={inferenceMessages.toolbar.progressLabel} className="text-sm">
          {inferenceMessages.toolbar.progress(counts, analysis.applied)}
        </p>
      </div>
    </div>
  );
}
