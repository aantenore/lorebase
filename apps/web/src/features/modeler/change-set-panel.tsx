import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import type { Schema, SchemaChange, SchemaChangeResult } from '@/api/types';
import { useScope } from '@/app/scope';
import { useConfig } from '@/config/context';
import { buildNames } from '@/features/review/conflict-meta';
import { modeler as modelerMessages } from '@/messages/modeler';
import type { SchemaDocument } from './document';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Badge, EmptyState, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { useToast } from '@/ui/toast';

/** One line per command: what it does and to which element. */
export function describeChange(change: SchemaChange, names: ReadonlyMap<string, string>): string {
  const [verb, ...rest] = change.command.split('_');
  const type = rest.join(' ');
  const element = change.arguments['element'];
  const added =
    typeof element === 'object' && element !== null && 'api_name' in element
      ? String(element.api_name)
      : undefined;
  const target =
    change.target === undefined ? undefined : (names.get(change.target) ?? change.target);
  const owner = change.arguments['owner'];
  const place =
    typeof owner === 'string' ? modelerMessages.changeSet.place(names.get(owner) ?? owner) : '';
  const set = change.arguments['set'];
  const fields =
    verb === 'update' && typeof set === 'object' && set !== null
      ? ` (${Object.keys(set).join(', ')})`
      : '';
  return `${verb} ${type} ${added ?? target ?? ''}${place}${fields}`.replace(/\s+/g, ' ').trim();
}

export interface ChangeSetPanelProps {
  base: Schema;
  next: Schema;
  changes: SchemaChange[];
  /** The whole edited schema; Keeper takes a change set in this form. */
  document: SchemaDocument;
  /** Why the change set cannot be saved yet, or `null`. */
  blocked: string | null;
  /** Result of the dry run on the Branch, while there is one. */
  check: { pending: boolean; result: SchemaChangeResult | undefined };
  /** Revision message and Proposal title used when the reviewer writes none. */
  defaultMessage?: string;
  /** Text of the two actions; the Modeler editor uses the defaults. */
  actionLabels?: { save: string; propose: string };
}

const t = modelerMessages.changeSet;
const DEFAULT_MESSAGE = t.defaultMessage;
const DEFAULT_LABELS = { save: t.save, propose: t.propose };

function Migrations({ result }: { result: SchemaChangeResult }) {
  if (result.migrations.length === 0) {
    return <p className="text-sm text-muted">{t.migrationsNone}</p>;
  }
  return (
    <Table caption={t.migrationsTitle}>
      <THead>
        <TR>
          <TH>{t.migration}</TH>
          <TH>{t.values}</TH>
          <TH>{t.failing}</TH>
        </TR>
      </THead>
      <TBody>
        {result.migrations.map((m) => (
          <TR key={m.description}>
            <TD>{m.description}</TD>
            <TD>{m.affected_values}</TD>
            <TD>{m.failing_values > 0 ? <Badge tone="danger">{m.failing_values}</Badge> : 0}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function slugOf(message: string): string {
  return message
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Change set summary (design 12.3): the pending commands, the data migrations the Branch
 * says they imply, and how to keep them. An unprotected Branch takes the change set as a
 * schema Revision; a protected one needs a new Branch and a Proposal.
 */
export function ChangeSetPanel({
  base,
  next,
  changes,
  document,
  blocked,
  check,
  defaultMessage = DEFAULT_MESSAGE,
  actionLabels = DEFAULT_LABELS,
}: ChangeSetPanelProps) {
  const api = useKeeperApi();
  const scope = useScope();
  const config = useConfig();
  const toast = useToast();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [message, setMessage] = useState('');
  const [branchName, setBranchName] = useState('');
  const protectedBranch = scope.branchInfo.protected;
  const names = new Map([...buildNames(base), ...buildNames(next)]);
  const revisionMessage = message.trim() === '' ? defaultMessage : message.trim();
  const proposedBranch =
    branchName.trim() === ''
      ? `${config.schemaBranchPrefix}${slugOf(revisionMessage)}`
      : branchName.trim();

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper'] });

  const save = useMutation({
    mutationFn: () =>
      api.schema.applyChanges(scope.domain, scope.branch, {
        changes,
        document,
        message: revisionMessage,
      }),
    onSuccess: (result) => {
      if (result.valid) toast.notify(t.saved, 'success');
      else toast.notify(t.refused, 'error');
      return refresh();
    },
  });

  const propose = useMutation({
    mutationFn: async () => {
      await api.branches.create(scope.domain, { name: proposedBranch, source: scope.branch });
      const result = await api.schema.applyChanges(scope.domain, proposedBranch, {
        changes,
        document,
        message: revisionMessage,
      });
      if (!result.valid) throw new Error(t.refused);
      return api.proposals.create(scope.domain, {
        source_ref: proposedBranch,
        target_ref: scope.branch,
        title: revisionMessage,
      });
    },
    onSuccess: async (proposal) => {
      toast.notify(t.proposalOpened(proposal.id), 'success');
      await refresh();
      await navigate({
        to: '/$domain/review/$proposal',
        params: { domain: scope.domain, proposal: proposal.id },
        search: {},
      });
    },
  });

  const working = save.isPending || propose.isPending;
  const disabled = blocked !== null || changes.length === 0 || working;

  return (
    <section aria-label={t.label} className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">
        {t.label} <Badge tone={changes.length === 0 ? 'neutral' : 'accent'}>{changes.length}</Badge>
      </h3>
      {changes.length === 0 ? (
        <EmptyState>{t.empty}</EmptyState>
      ) : (
        <>
          <ul className="list-disc pl-5 text-sm">
            {changes.map((change, index) => (
              <li key={`${change.command}-${change.target ?? index}-${index}`}>
                {describeChange(change, names)}
              </li>
            ))}
          </ul>
          <Section title={t.migrationsTitle}>
            {check.pending ? (
              <p role="status" className="text-sm text-muted">
                {t.migrationsChecking}
              </p>
            ) : check.result === undefined ? (
              <p className="text-sm text-muted">{t.migrationsBlocked}</p>
            ) : (
              <Migrations result={check.result} />
            )}
          </Section>
          <Input
            label={t.revisionMessage}
            value={message}
            placeholder={defaultMessage}
            onChange={(event) => {
              setMessage(event.target.value);
            }}
          />
          {protectedBranch && (
            <>
              <p className="text-sm text-muted">{t.protectedNotice}</p>
              <Input
                label={t.branchName}
                value={branchName}
                placeholder={proposedBranch}
                onChange={(event) => {
                  setBranchName(event.target.value);
                }}
              />
            </>
          )}
          {blocked !== null && <p className="text-sm text-danger">{blocked}</p>}
          <div className="flex gap-2">
            {protectedBranch ? (
              <Button
                variant="primary"
                disabled={disabled}
                onClick={() => {
                  propose.mutate();
                }}
              >
                {actionLabels.propose}
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={disabled}
                onClick={() => {
                  save.mutate();
                }}
              >
                {actionLabels.save}
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
