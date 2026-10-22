import { createFileRoute, Link } from '@tanstack/react-router';
import { useSession } from '@/api/provider';
import { Button } from '@/ui/button';

interface LoginSearch {
  error?: string | undefined;
  return_to?: string | undefined;
}

/** What the sign-in failure codes of Keeper (`/login?error=<code>`) mean for the person. */
const ERROR_MESSAGES: Readonly<Record<string, string>> = {
  login_expired: 'The sign-in took too long and expired. Sign in again.',
  login_failed: 'The sign-in did not complete. Sign in again.',
  access_denied: 'Access was denied by the identity provider.',
  no_account: 'Your account has no access to Lorebase. Ask an administrator to add you.',
  provider_unavailable: 'The identity provider is not reachable right now. Try again shortly.',
};

const UNKNOWN_ERROR = 'The sign-in failed.';

export const Route = createFileRoute('/login')({
  validateSearch: (raw: Record<string, unknown>): LoginSearch => ({
    error: typeof raw['error'] === 'string' && raw['error'] !== '' ? raw['error'] : undefined,
    return_to:
      typeof raw['return_to'] === 'string' && raw['return_to'] !== ''
        ? raw['return_to']
        : undefined,
  }),
  component: LoginScreen,
});

function LoginScreen() {
  const session = useSession();
  const { error, return_to: returnTo } = Route.useSearch();

  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="mb-4 text-xl font-semibold">Sign in to Lorebase</h1>
      {session === null ? (
        <>
          <p className="mb-4 text-sm text-muted">
            Sign-in is not used with this configuration: the app does not ask for a login.
          </p>
          <Link to="/" className="text-accent underline">
            Open Lorebase
          </Link>
        </>
      ) : (
        <>
          {error !== undefined && (
            <p role="alert" className="mb-4 text-danger">
              {Object.hasOwn(ERROR_MESSAGES, error) ? ERROR_MESSAGES[error] : UNKNOWN_ERROR}
            </p>
          )}
          <p className="mb-4 text-sm text-muted">
            Lorebase signs you in through your organization's identity provider.
          </p>
          <Button
            variant="primary"
            onClick={() => {
              window.location.assign(session.loginUrl(returnTo ?? '/'));
            }}
          >
            Sign in
          </Button>
        </>
      )}
    </main>
  );
}
