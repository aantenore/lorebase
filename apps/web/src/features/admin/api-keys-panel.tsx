import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import type { ApiKey, ApiKeyCreated } from '@/api/admin-types';
import { useKeeperApi } from '@/api/provider';
import { useApiKeys } from '@/api/queries';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';
import { Dialog } from '@/ui/dialog';
import { Input, Select } from '@/ui/field';
import { Badge, EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { useToast } from '@/ui/toast';
import { usePrincipalNames } from './principal-names';
import { DEFAULT_KEY_ROLE, ROLES } from './roles';

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
      aria-label="New API key"
      className="flex flex-col gap-2 rounded-md border border-warning bg-surface p-3"
    >
      <p className="text-sm font-medium">Copy the key now. It is shown only once.</p>
      <code className="font-mono text-sm break-all">{created.key}</code>
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(created.key).catch(() => undefined);
          }}
        >
          Copy key
        </Button>
        <Button size="sm" variant="primary" onClick={onDone}>
          I have copied it
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
    days !== '' && (!Number.isInteger(lifetime) || lifetime < 1)
      ? 'Whole days, at least 1.'
      : undefined;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };

  return (
    <form
      onSubmit={submit}
      className="flex flex-wrap items-start gap-3"
      aria-label="Create an API key"
    >
      <div className="w-56">
        <Input
          label="Name of the service principal"
          value={name}
          maxLength={120}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </div>
      <div className="w-44">
        <Select
          label="Role in this Domain"
          value={role}
          options={ROLES.map((value) => ({ value, label: value }))}
          onChange={(event) => {
            setRole(event.target.value);
          }}
        />
      </div>
      <div className="w-40">
        <Input
          label="Lifetime in days"
          type="number"
          min={1}
          value={days}
          placeholder="default"
          error={lifetimeError}
          onChange={(event) => {
            setDays(event.target.value);
          }}
        />
      </div>
      <Button
        type="submit"
        variant="primary"
        className="mt-5"
        disabled={name.trim() === '' || lifetimeError !== undefined || create.isPending}
      >
        Create key
      </Button>
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
      toast.notify(`API key ${key.prefix} revoked.`, 'success');
      setRevoking(null);
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'api-keys'] });
      await queryClient.invalidateQueries({ queryKey: ['keeper', 'audit'] });
    },
  });

  const now = new Date();
  return (
    <>
      <Section title="Create an API key">
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

      <Section title={`API keys of ${scope.domainInfo.display_name}`}>
        <QueryBoundary query={keys}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>No API keys in this Domain.</EmptyState>
            ) : (
              <Table caption="API keys">
                <THead>
                  <TR>
                    <TH>Key</TH>
                    <TH>Principal</TH>
                    <TH>Scopes</TH>
                    <TH>Created</TH>
                    <TH>Expires</TH>
                    <TH>Last used</TH>
                    <TH>Status</TH>
                    <TH>
                      <span className="sr-only">Actions</span>
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
                        <TD>{key.expires_at === null ? 'never' : shortTime(key.expires_at)}</TD>
                        <TD>{shortTime(key.last_used_at)}</TD>
                        <TD>
                          <Badge tone={STATUS_TONE[status]}>{status}</Badge>
                        </TD>
                        <TD>
                          {status !== 'revoked' && (
                            <Button
                              size="sm"
                              variant="danger"
                              aria-label={`Revoke ${key.prefix}`}
                              onClick={() => {
                                setRevoking(key);
                              }}
                            >
                              Revoke
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
        title="Revoke API key"
        onClose={() => {
          setRevoking(null);
        }}
      >
        {revoking !== null && (
          <div className="flex flex-col gap-3">
            <p className="text-sm">
              Revoke <code className="font-mono">{revoking.prefix}</code> held by{' '}
              {nameOf(revoking.principal_id)}? Whatever uses the key stops working at once, and this
              cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <Button
                onClick={() => {
                  setRevoking(null);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                disabled={revoke.isPending}
                onClick={() => {
                  revoke.mutate(revoking);
                }}
              >
                Revoke key
              </Button>
            </div>
          </div>
        )}
      </Dialog>
    </>
  );
}
