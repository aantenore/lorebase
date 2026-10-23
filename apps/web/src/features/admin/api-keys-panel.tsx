import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import type { ApiKey, ApiKeyCreated } from '@/api/admin-types';
import { useKeeperApi } from '@/api/provider';
import { useApiKeys } from '@/api/queries';
import { useScope } from '@/app/scope';
import { admin as adminMessages } from '@/messages/admin';
import { common as commonMessages } from '@/messages/common';
import { Button } from '@/ui/button';
import { Dialog } from '@/ui/dialog';
import { Input, Select } from '@/ui/field';
import { Badge, EmptyState, FormAction, FormRow, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { useToast } from '@/ui/toast';
import { usePrincipalNames } from './principal-names';
import { DEFAULT_KEY_ROLE, ROLES } from './roles';

const T = adminMessages.keys;

type KeyStatus = 'active' | 'revoked' | 'expired';

function statusOf(key: ApiKey, now: Date): KeyStatus {
  if (key.revoked_at !== null) return 'revoked';
  if (key.expires_at !== null && new Date(key.expires_at) <= now) return 'expired';
  return 'active';
}

const STATUS_TONE = { active: 'success', revoked: 'neutral', expired: 'warning' } as const;

function NewKeyNotice({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  return (
    <section
      aria-label={T.notice.label}
      className="flex flex-col gap-2 rounded-md border border-warning bg-surface p-3"
    >
      <p className="text-sm font-medium">{T.notice.warning}</p>
      <code className="font-mono text-sm break-all">{created.key}</code>
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(created.key).catch(() => undefined);
          }}
        >
          {T.notice.copy}
        </Button>
        <Button size="sm" variant="primary" onClick={onDone}>
          {T.notice.done}
        </Button>
      </div>
    </section>
  );
}

function CreateKeyForm({ onCreated }: { onCreated: (key: ApiKeyCreated) => void }) {
  const api = useKeeperApi();
  const scope = useScope();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [role, setRole] = useState<string>(DEFAULT_KEY_ROLE);
  const [days, setDays] = useState('');

  const create = useMutation({
    mutationFn: () =>
      api.admin.apiKeys.create(scope.domain, {
        name: name.trim(),
        role,
        ...(days === '' ? {} : { ttl_days: Number(days) }),
      }),
    onSuccess: async (created) => {
      setName('');
      setDays('');
      onCreated(created);
      for (const key of ['api-keys', 'principals', 'grants', 'audit']) {
        await queryClient.invalidateQueries({ queryKey: ['keeper', key] });
      }
    },
  });

  const lifetime = Number(days);
  const lifetimeError =
    days !== '' && (!Number.isInteger(lifetime) || lifetime < 1) ? T.form.lifetimeError : undefined;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };

  return (
    <form onSubmit={submit} aria-label={T.form.label}>
      <FormRow>
        <div className="w-56">
          <Input
            label={T.form.name}
            value={name}
            maxLength={120}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </div>
        <div className="w-44">
          <Select
            label={T.form.role}
            value={role}
            options={ROLES.map((value) => ({ value, label: value }))}
            onChange={(event) => {
              setRole(event.target.value);
            }}
          />
        </div>
        <div className="w-40">
          <Input
            label={T.form.lifetime}
            type="number"
            min={1}
            value={days}
            placeholder={T.form.lifetimePlaceholder}
            error={lifetimeError}
            onChange={(event) => {
              setDays(event.target.value);
            }}
          />
        </div>
        <FormAction>
          <Button
            type="submit"
            variant="primary"
            disabled={name.trim() === '' || lifetimeError !== undefined || create.isPending}
          >
            {T.form.submit}
          </Button>
        </FormAction>
      </FormRow>
    </form>
  );
}

/** API keys of the Domain: list with last use, create (the secret shows once) and revoke. */
export function ApiKeysPanel() {
  const scope = useScope();
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const keys = useApiKeys(scope.domain);
  const nameOf = usePrincipalNames();
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const revoke = useMutation({
    mutationFn: (key: ApiKey) => api.admin.apiKeys.revoke(scope.domain, key.id),
    onSuccess: async (_result, key) => {
      toast.notify(T.revoked(key.prefix), 'success');
      setRevoking(null);
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'api-keys'] });
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'audit'] });
    },
  });

  const now = new Date();
  return (
    <>
      <Section title={T.createTitle}>
        <div className="flex flex-col gap-3">
          {created !== null && (
            <NewKeyNotice
              created={created}
              onDone={() => {
                setCreated(null);
              }}
            />
          )}
          <CreateKeyForm onCreated={setCreated} />
        </div>
      </Section>

      <Section title={T.listTitle}>
        <QueryBoundary query={keys}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>{T.none}</EmptyState>
            ) : (
              <Table caption={T.caption}>
                <THead>
                  <TR>
                    <TH>{T.columns.key}</TH>
                    <TH>{T.columns.principal}</TH>
                    <TH>{T.columns.scopes}</TH>
                    <TH>{T.columns.created}</TH>
                    <TH>{T.columns.expires}</TH>
                    <TH>{T.columns.lastUsed}</TH>
                    <TH>{T.columns.status}</TH>
                    <TH>
                      <span className="sr-only">{T.columns.actions}</span>
                    </TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((key) => {
                    const status = statusOf(key, now);
                    return (
                      <TR key={key.id}>
                        <TD className="font-mono text-xs">{key.prefix}...</TD>
                        <TD>{nameOf(key.principal_id)}</TD>
                        <TD>{key.scopes.join(', ')}</TD>
                        <TD>{shortTime(key.created_at)}</TD>
                        <TD>{key.expires_at === null ? T.never : shortTime(key.expires_at)}</TD>
                        <TD>{shortTime(key.last_used_at)}</TD>
                        <TD>
                          <Badge tone={STATUS_TONE[status]}>{T.status[status]}</Badge>
                        </TD>
                        <TD>
                          {status !== 'revoked' && (
                            <Button
                              size="sm"
                              variant="danger"
                              aria-label={T.revokeLabel(key.prefix)}
                              onClick={() => {
                                setRevoking(key);
                              }}
                            >
                              {T.revoke}
                            </Button>
                          )}
                        </TD>
                      </TR>
                    );
                  })}
                </TBody>
              </Table>
            )
          }
        </QueryBoundary>
      </Section>

      <Dialog
        open={revoking !== null}
        title={T.dialog.title}
        onClose={() => {
          setRevoking(null);
        }}
      >
        {revoking !== null && (
          <div className="flex flex-col gap-3">
            <p className="text-sm">
              {T.revoke} <code className="font-mono">{revoking.prefix}</code>{' '}
              {T.dialog.heldBy(nameOf(revoking.principal_id))}
            </p>
            <div className="flex justify-end gap-2">
              <Button
                onClick={() => {
                  setRevoking(null);
                }}
              >
                {commonMessages.cancel}
              </Button>
              <Button
                variant="danger"
                disabled={revoke.isPending}
                onClick={() => {
                  revoke.mutate(revoking);
                }}
              >
                {T.dialog.confirm}
              </Button>
            </div>
          </div>
        )}
      </Dialog>
    </>
  );
}
