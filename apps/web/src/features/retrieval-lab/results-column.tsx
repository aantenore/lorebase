import type { UseQueryResult } from '@tanstack/react-query';
import type { SearchResponse } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';
import { cn } from '@/ui/cn';
import { Badge, EmptyState } from '@/ui/layout';
import { correctionText } from '@/features/agent-activity/step-view';
import { describeRow, type Comparison } from './result-diff';

const R = retrievalLabMessages.results;

/**
 * The hits of one parameter set. Snippets come from the Keeper's text search and may hold
 * markup-like text; they are rendered as text only.
 */
export function ResultsColumn({
  name,
  domain,
  query,
  comparison,
}: {
  name: 'A' | 'B';
  domain: string;
  query: UseQueryResult<SearchResponse>;
  comparison: Comparison | null;
}) {
  return (
    <section aria-label={R.column(name)}>
      <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
        {R.column(name)}
        {query.data?.mode !== undefined && <Badge>{query.data.mode}</Badge>}
      </h3>
      {query.isPending && !query.isFetching ? (
        <EmptyState>{R.askFirst}</EmptyState>
      ) : query.isPending ? (
        <p role="status" className="text-muted">
          {R.searching}
        </p>
      ) : query.isError ? (
        <p role="alert" className="text-danger">
          {query.error.message}
        </p>
      ) : (
        <>
          {(query.data.corrections ?? []).length > 0 && (
            <p className="mb-2 text-xs">
              {R.corrected}{' '}
              {(query.data.corrections ?? []).map((c, index) => (
                <span key={c.word}>
                  {index > 0 && '; '}
                  <code className="font-mono">{correctionText(c)}</code>
                </span>
              ))}
            </p>
          )}
          {query.data.results.length === 0 ? (
            <EmptyState>{R.noHits}</EmptyState>
          ) : (
            <ol className="flex flex-col gap-2">
              {query.data.results.map((hit, index) => {
                const row = comparison?.rows.get(hit.ref);
                const shared = row !== undefined && row.rankA !== null && row.rankB !== null;
                return (
                  <li
                    key={hit.ref}
                    className={cn(
                      'rounded-md border bg-surface p-2 text-sm',
                      shared ? 'border-line' : 'border-warning',
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="w-6 font-mono text-xs text-muted">{index + 1}.</span>
                      <RecordLink domain={domain} recordRef={hit.ref}>
                        {hit.title}
                      </RecordLink>
                      <Badge>{hit.kind}</Badge>
                      <span className="ml-auto font-mono text-xs">{R.score(hit.score)}</span>
                    </div>
                    {hit.snippet !== '' && <p className="mt-1 text-xs text-muted">{hit.snippet}</p>}
                    {row !== undefined && (
                      <p className="mt-1 text-xs text-muted">{describeRow(row, name)}</p>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
