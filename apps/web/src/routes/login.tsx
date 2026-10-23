import { createFileRoute, Link } from '@tanstack/react-router';
import { useSession } from '@/api/provider';
import { shell as shellMessages } from '@/messages/shell';
import { Button } from '@/ui/button';

interface LoginSearch {
  error?: string | undefined;
  return_to?: string | undefined;
}

/** What the sign-in failure codes of Keeper (`/login?error=<code>`) mean for the person. */
const COPY = shellMessages.login;

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
      <h1 className="mb-4 text-xl font-semibold">{COPY.title}</h1>
      {session === null ? (
        <>
          <p className="mb-4 text-sm text-muted">{COPY.notUsed}</p>
          <Link to="/" className="text-accent underline">
            {COPY.open}
          </Link>
        </>
      ) : (
        <>
          {error !== undefined && (
            <p role="alert" className="mb-4 text-danger">
              {Object.hasOwn(COPY.errors, error) ? COPY.errors[error] : COPY.unknownError}
            </p>
          )}
          <p className="mb-4 text-sm text-muted">{COPY.intro}</p>
          <Button
            variant="primary"
            onClick={() => {
              window.location.assign(session.loginUrl(returnTo ?? '/'));
            }}
          >
            {COPY.submit}
          </Button>
        </>
      )}
    </main>
  );
}
