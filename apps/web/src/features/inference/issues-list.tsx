import type { InferenceIssue } from '@/api/inference-types';
import type { SchemaIssue } from '@/api/schema-rules';
import { Badge } from '@/ui/layout';

export interface IssuesListProps {
  /** What the inference reported. */
  issues: readonly InferenceIssue[];
  /** How many Keeper counted; it lists a capped number. */
  issueCount: number;
  /** Problems of the schema the accepted elements would produce. */
  validation: readonly SchemaIssue[];
}

/** The ambiguities the inference found and the rule violations of the edited proposal. */
export function IssuesList({ issues, issueCount, validation }: IssuesListProps) {
  return (
    <section aria-label="Issues" className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">
        Inference issues <Badge tone={issueCount === 0 ? 'success' : 'warning'}>{issueCount}</Badge>
      </h3>
      {issues.length === 0 ? (
        <p className="text-sm text-muted">The inference found nothing to settle.</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {issues.map((issue, index) => (
            <li key={`${issue.code}-${index.toString()}`} className="flex flex-wrap gap-2">
              <Badge tone={issue.severity === 'error' ? 'danger' : 'warning'}>
                {issue.severity}
              </Badge>
              <code className="font-mono text-xs">{issue.code}</code>
              <span>{issue.message}</span>
            </li>
          ))}
        </ul>
      )}
      {issueCount > issues.length && (
        <p className="text-xs text-muted">
          Showing {issues.length} of {issueCount} issues.
        </p>
      )}
      {validation.length > 0 && (
        <>
          <h3 className="text-sm font-semibold">
            Schema problems <Badge tone="danger">{validation.length}</Badge>
          </h3>
          <ul className="flex flex-col gap-1 text-sm" aria-label="Schema problems">
            {validation.map((issue, index) => (
              <li
                key={`${issue.path}-${issue.code}-${index.toString()}`}
                className="flex flex-wrap gap-2"
              >
                <code className="font-mono text-xs">{issue.path || 'schema'}</code>
                <span className="text-danger">{issue.message}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
