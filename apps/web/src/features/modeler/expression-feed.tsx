import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import type { JsonValue } from '@/api/types';
import { useKeeperApi } from '@/api/provider';
import { useScope } from '@/app/scope';
import { modeler as modelerMessages } from '@/messages/modeler';
import type { SchemaDocument } from './document';
import type { ExpressionResults, Row } from './source-preview';

/** How long an edit rests before the sample rows are sent to Keeper again. */
const EDIT_DELAY_MS = 400;

function useRested<T>(value: T): T {
  const [rested, setRested] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => {
      setRested(value);
    }, EDIT_DELAY_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [value]);
  return rested;
}

export interface ExpressionEvaluation {
  /** Keeper's results for the rows, for the mapping as it was last evaluated. */
  results: ExpressionResults | undefined;
  /** Why Keeper could not evaluate the expressions (an invalid edit, a missing permission). */
  error: string | undefined;
}

/**
 * Asks Keeper to evaluate the expression mappings of Source `name` in the edited `document` on
 * the sample `rows`, and reports what it answers. Only the rows and the document are sent;
 * nothing is read from the Source and nothing is written. Renders nothing.
 */
export function ExpressionFeed({
  name,
  document,
  rows,
  onChange,
}: {
  name: string;
  document: SchemaDocument;
  rows: Row[];
  onChange: (evaluation: ExpressionEvaluation) => void;
}) {
  const api = useKeeperApi();
  const scope = useScope();
  const rested = useRested(document);
  const query = useQuery({
    queryKey: ['keeper', 'source-preview', scope.domain, scope.branch, name, rested, rows],
    queryFn: async (): Promise<ExpressionResults> => {
      const answer = await api.sources.previewExpressions(scope, {
        document: rested,
        source: name,
        rows: rows as Record<string, JsonValue>[],
      });
      return answer.rows.map((row) =>
        Object.fromEntries(
          Object.entries(row).map(([attribute, cell]) => [
            attribute,
            cell.error === null
              ? { value: cell.value }
              : {
                  error:
                    cell.error.message ?? modelerMessages.expressions.notEvaluated(cell.error.code),
                },
          ]),
        ),
      );
    },
    retry: false,
    placeholderData: keepPreviousData,
    staleTime: Infinity,
  });
  const results = query.data;
  const error = query.isError ? query.error.message : undefined;
  useEffect(() => {
    onChange({ results, error });
  }, [results, error, onChange]);
  return null;
}
