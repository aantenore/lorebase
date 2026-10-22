import { Link } from '@tanstack/react-router';
import { useState, type FormEvent } from 'react';
import type { TraceSession } from '@/api/types';
import { useScopeSearch } from '@/app/scope';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Badge, EmptyState } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { InferredBadge, ProposalLink } from './proposal-link';

/** Filter by principal; applied when submitted, so a request is not sent per keystroke. */
export function ActorFilter({
  actor,
  onActor,
}: {
  actor: string | undefined;
  onActor: (actor: string | undefined) => void;
}) {
  const [text, setText] = useState(actor ?? '');
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onActor(text.trim() === '' ? undefined : text.trim());
  };
  return (
    <form onSubmit={submit} className="mb-3 flex items-end gap-2">
      <Input
        label="Actor"
        value={text}
        placeholder="svc_herald"
        onChange={(event) => {
          setText(event.target.value);
        }}
        className="w-64"
      />
      <Button type="submit">Filter</Button>
    </form>
  );
}

/** The sessions Keeper traced, newest activity first; a row opens the timeline of the session. */
export function SessionsList({
  domain,
  sessions,
  selected,
}: {
  domain: string;
  sessions: TraceSession[];
  selected: string | undefined;
}) {
  const scopeSearch = useScopeSearch();
  if (sessions.length === 0) return <EmptyState>No agent sessions match.</EmptyState>;
  return (
    <Table caption="Agent sessions">
      <THead>
        <tr>
          <TH>Session</TH>
          <TH>Actor</TH>
          <TH>Last activity</TH>
          <TH>Calls</TH>
          <TH>Notes</TH>
          <TH>Tools</TH>
          <TH>Proposals</TH>
        </tr>
      </THead>
      <TBody>
        {sessions.map((session) => (
          <TR
            key={session.session_id}
            className={session.session_id === selected ? 'bg-surface-2' : ''}
          >
            <TD>
              <Link
                to="/$domain/agent-activity"
                params={{ domain }}
                search={{ ...scopeSearch, session: session.session_id, actor: session.actor }}
                aria-current={session.session_id === selected ? 'true' : undefined}
                className="font-mono text-xs text-accent hover:underline"
              >
                {session.session_id}
              </Link>
              {session.inferred && <InferredBadge />}
            </TD>
            <TD>{session.actor}</TD>
            <TD className="whitespace-nowrap">{shortTime(session.last_at)}</TD>
            <TD>{session.calls}</TD>
            <TD>{session.notes}</TD>
            <TD>
              <span className="flex flex-wrap gap-1">
                {session.tools.map((tool) => (
                  <Badge key={tool}>{tool}</Badge>
                ))}
              </span>
            </TD>
            <TD>
              <span className="flex flex-wrap gap-2">
                {session.proposals.length === 0 && <span className="text-muted">none</span>}
                {session.proposals.map((proposal) => (
                  <ProposalLink key={proposal} domain={domain} proposal={proposal} />
                ))}
              </span>
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
