import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, useSyncExternalStore } from 'react';
import { describeError } from '@/api';
import { useCredentials, useKeeperApi } from '@/api/provider';
import { useConfig } from '@/config/context';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { EmptyState, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { useToast } from '@/ui/toast';

const SOURCE_LABEL = {
  storage: 'typed here (kept in this browser)',
  env: 'VITE_API_TOKEN',
  none: 'none',
} as const;

function Row({ name, value }: { name: string; value: string }) {
  return (
    <TR>
      <TH scope="row" className="font-normal">
        {name}
      </TH>
      <TD>{value}</TD>
    </TR>
  );
}

/** Who Keeper says the caller is, for the credential this browser sends. */
function WhoAmI() {
  const api = useKeeperApi();
  const credentials = useCredentials();
  const source = useSyncExternalStore(credentials.subscribe, credentials.source);
  const who = useQuery({
    queryKey: ['keeper', 'whoami', source],
    queryFn: () => api.auth.whoami(),
  });
  if (who.isPending) {
    return (
      <p role="status" className="text-sm text-muted">
        Asking Keeper who you are...
      </p>
    );
  }
  if (who.isError) {
    return (
      <EmptyState>{`Keeper did not identify the caller. ${describeError(who.error)}`}</EmptyState>
    );
  }
  const principal = who.data;
  return (
    <Table caption="Caller">
      <THead>
        <TR>
          <TH>Property</TH>
          <TH>Value</TH>
        </TR>
      </THead>
      <TBody>
        <Row name="Principal" value={principal.display_name} />
        <Row name="Id" value={principal.id} />
        <Row name="Type" value={principal.type} />
        <Row name="Authenticated by" value={principal.method} />
        <Row
          name="Installation administrator"
          value={principal.installation_admin ? 'yes' : 'no'}
        />
        <Row name="Key scopes" value={principal.key_scopes.join(', ') || 'none'} />
      </TBody>
    </Table>
  );
}

/**
 * Connection: where Keeper is, which token this browser sends, and who Keeper says it is.
 * The token field is a development convenience; it keeps the token in local storage, in the
 * clear (README, "Authentication").
 */
export function ConnectionSection() {
  const config = useConfig();
  const credentials = useCredentials();
  const toast = useToast();
  const queryClient = useQueryClient();
  const source = useSyncExternalStore(credentials.subscribe, credentials.source);
  const [token, setToken] = useState('');
  const live = config.apiMode === 'live';

  const apply = (next: string | null) => {
    credentials.set(next);
    setToken('');
    toast.notify(
      next === null ? 'Stored token forgotten.' : 'Token stored in this browser.',
      'success',
    );
    return queryClient.invalidateQueries({ queryKey: ['keeper'] });
  };

  return (
    <Section title="Connection">
      <Table caption="Connection">
        <THead>
          <TR>
            <TH>Setting</TH>
            <TH>Value</TH>
          </TR>
        </THead>
        <TBody>
          <Row name="Data source" value={live ? 'Keeper over REST' : 'sample data (mock)'} />
          <Row
            name="Keeper"
            value={
              live
                ? config.keeperBaseUrl === ''
                  ? 'this origin (/api/v1)'
                  : (config.keeperBaseUrl ?? '')
                : 'not used in mock mode'
            }
          />
          <Row name="API token" value={live ? SOURCE_LABEL[source] : 'not used in mock mode'} />
        </TBody>
      </Table>

      {live && (
        <form
          aria-label="Store an API token"
          className="mt-3 flex max-w-xl flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void apply(token.trim() === '' ? null : token);
          }}
        >
          <Input
            label="API token"
            type="password"
            autoComplete="off"
            value={token}
            hint="API key (lbk_...) or access token, sent as a Bearer header. Development only: it is kept in this browser's local storage, unencrypted."
            onChange={(event) => {
              setToken(event.target.value);
            }}
          />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" disabled={token.trim() === ''}>
              Save token
            </Button>
            <Button
              type="button"
              disabled={source !== 'storage'}
              onClick={() => {
                void apply(null);
              }}
            >
              Forget stored token
            </Button>
          </div>
        </form>
      )}

      <div className="mt-4">
        <h3 className="mb-2 text-sm font-semibold">Who am I</h3>
        <WhoAmI />
      </div>
    </Section>
  );
}
