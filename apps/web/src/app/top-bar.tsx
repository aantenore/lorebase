import { Link } from '@tanstack/react-router';
import { Select } from '@/ui/field';
import { useTheme, type ThemeChoice } from '@/ui/theme';
import { BranchSwitcher } from './branch-switcher';
import { DomainSwitcher } from './domain-switcher';
import { GlobalSearch } from './global-search';
import type { ScopeSearch } from './scope';

const THEMES: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'System theme' },
  { value: 'light', label: 'Light theme' },
  { value: 'dark', label: 'Dark theme' },
];

function ThemeSelect() {
  const { theme, setTheme } = useTheme();
  return (
    <Select
      label="Theme"
      hideLabel
      value={theme}
      options={THEMES}
      onChange={(event) => {
        setTheme(event.target.value as ThemeChoice);
      }}
    />
  );
}

export function TopBar({ domain, search }: { domain: string; search: ScopeSearch }) {
  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-2">
      <Link to="/" className="mr-2 text-base font-semibold">
        Lorebase
      </Link>
      <DomainSwitcher domain={domain} />
      <BranchSwitcher domain={domain} search={search} />
      <div className="ml-auto flex items-center gap-2">
        <GlobalSearch domain={domain} search={search} />
        <ThemeSelect />
      </div>
    </header>
  );
}
