import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, useSyncExternalStore } from 'react';
import { describeError } from '@/api';
import { useCredentials, useKeeperApi, useSession, useSessionInfo } from '@/api/provider';
import { useConfig } from '@/config/context';
import { settings as settingsMessages } from '@/messages/settings';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { EmptyState, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { useToast } from '@/ui/toast';

const T = settingsMessages;
const SOURCE_LABEL = T.connection.tokenSource;

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
        {T.whoAmI.asking}
      </p>
    );
  }
  if (who.isError) {
    return <EmptyState>{T.whoAmI.failed(describeError(who.error))}</EmptyState>;
  }
  const principal = who.data;
  return (
    <Table caption={T.whoAmI.caption}>
      <THead>
        <TR>
          <TH>{T.table.property}</TH>
          <TH>{T.table.value}</TH>
        </TR>
      </THead>
      <TBody>
        <Row name={T.whoAmI.principal} value={principal.display_name} />
        <Row name={T.whoAmI.id} value={principal.id} />
        <Row name={T.whoAmI.type} value={principal.type} />
        <Row name={T.whoAmI.method} value={principal.method} />
        <Row
          name={T.whoAmI.admin}
          value={principal.installation_admin ? T.whoAmI.yes : T.whoAmI.no}
        />
        <Row name={T.whoAmI.keyScopes} value={principal.key_scopes.join(', ') || T.whoAmI.none} />
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
  const session = useSession();
  const signedIn = useSessionInfo();
  const toast = useToast();
  const queryClient = useQueryClient();
  const source = useSyncExternalStore(credentials.subscribe, credentials.source);
  const [token, setToken] = useState('');
  const live = config.apiMode === 'live';

  const apply = (next: string | null) => {
    credentials.set(next);
    setToken('');
    toast.notify(next === null ? T.connection.forgotten : T.connection.stored, 'success');
    return queryClient.invalidateQueries({ queryKey: ['keeper'] });
  };

  return (
    <Section title={T.connection.title}>
      <Table caption={T.connection.caption}>
        <THead>
          <TR>
            <TH>{T.table.setting}</TH>
            <TH>{T.table.value}</TH>
          </TR>
        </THead>
        <TBody>
          <Row
            name={T.connection.dataSource}
            value={live ? T.connection.keeperRest : T.connection.sampleData}
          />
          <Row
            name={T.connection.keeper}
            value={
              live
                ? config.keeperBaseUrl === ''
                  ? T.connection.thisOriginApi
                  : (config.keeperBaseUrl ?? '')
                : T.connection.notUsedInMock
            }
          />
          {session === null ? (
            <Row
              name={T.connection.apiToken}
              value={live ? SOURCE_LABEL[source] : T.connection.notUsedInMock}
            />
          ) : (
            <Row
              name={T.connection.signIn}
              value={T.connection.singleSignOn(signedIn?.display_name ?? null)}
            />
          )}
        </TBody>
      </Table>

      {session === null && live && (
        <form
          aria-label={T.connection.form}
          className="mt-3 flex max-w-xl flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void apply(token.trim() === '' ? null : token);
          }}
        >
          <Input
            label={T.connection.apiToken}
            type="password"
            autoComplete="off"
            value={token}
            hint={T.connection.tokenHint}
            onChange={(event) => {
              setToken(event.target.value);
            }}
          />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" disabled={token.trim() === ''}>
              {T.connection.save}
            </Button>
            <Button
              type="button"
              disabled={source !== 'storage'}
              onClick={() => {
                void apply(null);
              }}
            >
              {T.connection.forget}
            </Button>
          </div>
        </form>
      )}

      {session !== null && (
        <p className="mt-3 max-w-xl text-sm text-muted">{T.connection.noTokenInBrowser}</p>
      )}

      <div className="mt-4">
        <h3 className="mb-2 text-sm font-semibold">{T.whoAmI.title}</h3>
        <WhoAmI />
      </div>
    </Section>
  );
}
