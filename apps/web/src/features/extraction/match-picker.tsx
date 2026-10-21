import { useId } from 'react';
import type { ExtractionItem } from '@/api/extraction-types';
import { percent } from './item-state';

/** The Record the item points at, or `null`: the key of a Record or value item, the `to` of a Link. */
export const matchedKey = (item: ExtractionItem): string | null =>
  item.type === 'link' ? item.to : item.key;

/**
 * Candidates of an ambiguous match. Choosing one stages the edit that points the item at
 * that Record (`key` for Records and values, `to` for Links); the best-scored one is the
 * default Keeper proposed.
 */
export function MatchPicker({
  item,
  disabled,
  onPick,
}: {
  item: ExtractionItem;
  disabled: boolean;
  onPick: (key: string) => void;
}) {
  const name = useId();
  const candidates = item.match?.candidates ?? [];
  if (candidates.length < 2 && item.match?.status !== 'ambiguous') return null;
  const chosen = matchedKey(item);
  return (
    <fieldset disabled={disabled} className="flex flex-col gap-1">
      <legend className="mb-1 text-xs font-medium text-muted">
        {item.match?.status === 'ambiguous'
          ? 'Which Record is meant?'
          : 'Other Records it could mean'}
      </legend>
      {candidates.map((candidate) => (
        <label key={candidate.key} className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name={`candidate-${name}`}
            className="mt-1"
            checked={chosen === candidate.key}
            onChange={() => {
              onPick(candidate.key);
            }}
          />
          <span>
            <span className="font-medium">{candidate.title}</span>{' '}
            <span className="font-mono text-xs">{candidate.key}</span>
            <span className="block text-xs text-muted">
              {percent(candidate.score)} by {candidate.method}
            </span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}
