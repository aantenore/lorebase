import type { SchemaIssue } from '@/api/schema-rules';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import { locateIssue, type Position, type SyntaxIssue } from './yaml';

export interface IssuesPanelProps {
  text: string;
  syntaxIssues: SyntaxIssue[];
  issues: SchemaIssue[];
  /** Problems the Branch reported on a dry run, for example failing casts. */
  remote?: SchemaIssue[];
  onJump: (position: Position) => void;
}

function Item({
  path,
  message,
  code,
  position,
  onJump,
}: {
  path: string;
  message: string;
  code: string;
  position: Position | null;
  onJump: (position: Position) => void;
}) {
  return (
    <li className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <Badge tone="danger">{code}</Badge>
      {path !== '' && <code className="font-mono text-xs">{path}</code>}
      <span>{message}</span>
      {position !== null && (
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          aria-label={`Go to line ${position.line}: ${message}`}
          onClick={() => {
            onJump(position);
          }}
        >
          Line {position.line}
        </Button>
      )}
    </li>
  );
}

/** Validation issues of the document, each with its path and the line it sits on. */
export function IssuesPanel({ text, syntaxIssues, issues, remote = [], onJump }: IssuesPanelProps) {
  const total = syntaxIssues.length + issues.length + remote.length;
  if (total === 0) {
    return (
      <p role="status" className="text-sm text-success">
        No validation issues.
      </p>
    );
  }
  return (
    <section aria-label="Validation issues">
      <h3 className="mb-1 text-sm font-semibold">Validation issues ({total})</h3>
      <ul className="divide-y divide-line rounded-md border border-line bg-surface">
        {syntaxIssues.map((issue, index) => (
          <Item
            key={`syntax-${index}`}
            path=""
            code={issue.code}
            message={issue.message}
            position={{ line: issue.line, col: issue.col }}
            onJump={onJump}
          />
        ))}
        {syntaxIssues.length === 0 &&
          issues.map((issue, index) => (
            <Item
              key={`${issue.path}-${issue.code}-${index}`}
              path={issue.path}
              code={issue.code}
              message={issue.message}
              position={locateIssue(text, issue.path)}
              onJump={onJump}
            />
          ))}
        {syntaxIssues.length === 0 &&
          remote.map((issue, index) => (
            <Item
              key={`remote-${issue.path}-${index}`}
              path={issue.path}
              code={issue.code}
              message={issue.message}
              position={locateIssue(text, issue.path)}
              onJump={onJump}
            />
          ))}
      </ul>
    </section>
  );
}
