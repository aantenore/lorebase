import { Link } from '@tanstack/react-router';
import { useSession, useSessionInfo } from '@/api/provider';
import { LOGIN_PATH } from '@/api/session';
import { shell as shellMessages } from '@/messages/shell';
import { Button } from '@/ui/button';
import { Select } from '@/ui/field';
import { useTheme, type ThemeChoice } from '@/ui/theme';
import { BranchSwitcher } from './branch-switcher';
import { DomainSwitcher } from './domain-switcher';
import { GlobalSearch } from './global-search';
import type { ScopeSearch } from './scope';

const THEMES: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: shellMessages.theme.system },
  { value: 'light', label: shellMessages.theme.light },
  { value: 'dark', label: shellMessages.theme.dark },
];

function ThemeSelect() {
  const { theme, setTheme } = useTheme();
  return (
    <Select
      label={shellMessages.theme.label}
      hideLabel
      className="w-36"
      value={theme}
      options={THEMES}
      onChange={(event) => {
        setTheme(event.target.value as ThemeChoice);
      }}
    />
  );
}

/** Who is signed in and the way out; shown only when the app authenticates by session. */
function SessionMenu() {
  const session = useSession();
  const info = useSessionInfo();
  if (session === null || info === null) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="max-w-40 truncate text-sm text-muted">{info.display_name}</span>
      <Button
        size="sm"
        onClick={() => {
          void session.logout().then((url) => {
            window.location.assign(url ?? LOGIN_PATH);
          });
        }}
      >
        {shellMessages.signOut}
      </Button>
    </div>
  );
}

export function TopBar({ domain, search }: { domain: string; search: ScopeSearch }) {
  return (
    <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line bg-surface px-4 py-2">
      <Link to="/" className="mr-1 text-base leading-9 font-semibold">
        {shellMessages.product}
      </Link>
      <DomainSwitcher domain={domain} />
      <BranchSwitcher domain={domain} search={search} />
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <GlobalSearch domain={domain} search={search} />
        <ThemeSelect />
        <SessionMenu />
      </div>
    </header>
  );
}
