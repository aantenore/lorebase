import { useNavigate } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { useConfig } from '@/config/context';
import { useScope, useScopeSearch } from '@/app/scope';
import { settings as settingsMessages } from '@/messages/settings';
import { AccessPanel } from '@/features/admin/access-panel';
import { ApiKeysPanel } from '@/features/admin/api-keys-panel';
import { AuditPanel } from '@/features/admin/audit-panel';
import { ConnectionSection } from './connection-section';
import { Select } from '@/ui/field';
import { PageHeader, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';
import { useTheme, type ThemeChoice } from '@/ui/theme';
import { isSettingsSection, SETTINGS_SECTIONS, type SettingsSection } from './search';

const T = settingsMessages;

function Row({ name, value }: { name: string; value: string | number }) {
  return (
    <TR>
      <TH scope="row" className="font-normal">
        {name}
      </TH>
      <TD>{value}</TD>
    </TR>
  );
}

const ingestionMode = (settings: Record<string, unknown>): string => {
  const extraction = settings['extraction'];
  if (typeof extraction === 'object' && extraction !== null && 'mode' in extraction) {
    const mode = String(extraction.mode);
    return mode in T.domain.modes ? T.domain.modes[mode as keyof typeof T.domain.modes] : mode;
  }
  return T.domain.installationDefault;
};

const searchLanguage = (settings: Record<string, unknown>): string => {
  const language = settings['search_language'];
  return typeof language === 'string' ? language : T.domain.defaultLanguage;
};

function General() {
  const scope = useScope();
  const config = useConfig();
  const { theme, setTheme } = useTheme();
  const { domainInfo } = scope;

  return (
    <>
      <Section title={T.appearance.title}>
        <div className="w-48">
          <Select
            label={T.appearance.theme}
            value={theme}
            options={[
              { value: 'system', label: T.appearance.system },
              { value: 'light', label: T.appearance.light },
              { value: 'dark', label: T.appearance.dark },
            ]}
            onChange={(event) => {
              setTheme(event.target.value as ThemeChoice);
            }}
          />
        </div>
      </Section>

      <ConnectionSection />

      <Section title={T.domain.title}>
        <Table caption={T.domain.caption}>
          <THead>
            <TR>
              <TH>{T.table.setting}</TH>
              <TH>{T.table.value}</TH>
            </TR>
          </THead>
          <TBody>
            <Row name={T.domain.id} value={domainInfo.id} />
            <Row name={T.domain.defaultBranch} value={domainInfo.default_branch} />
            <Row name={T.domain.createdBy} value={domainInfo.created_by} />
            <Row name={T.domain.searchLanguage} value={searchLanguage(domainInfo.settings)} />
            <Row name={T.domain.ingestionMode} value={ingestionMode(domainInfo.settings)} />
          </TBody>
        </Table>
        {(domainInfo.ignored_settings ?? []).length > 0 && (
          <div role="group" aria-label={T.domain.ignoredLabel} className="mt-3 text-sm">
            <p className="text-muted">{T.domain.ignoredExplanation}</p>
            <ul className="mt-1 list-disc pl-5">
              {(domainInfo.ignored_settings ?? []).map((path) => (
                <li key={path} className="font-mono text-xs">
                  {path}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section title={T.runtime.title}>
        <Table caption={T.runtime.caption}>
          <THead>
            <TR>
              <TH>{T.table.setting}</TH>
              <TH>{T.table.value}</TH>
            </TR>
          </THead>
          <TBody>
            <Row name={T.runtime.dataSource} value={config.apiMode} />
            <Row
              name={T.runtime.keeperBaseUrl}
              value={
                config.keeperBaseUrl === ''
                  ? T.runtime.thisOrigin
                  : (config.keeperBaseUrl ?? T.runtime.notUsedInMock)
              }
            />
            <Row name={T.runtime.pageSize} value={config.pageSize} />
            <Row name={T.runtime.searchLimit} value={config.searchLimit} />
            <Row name={T.runtime.canvasDepth} value={config.canvas.depth} />
            <Row name={T.runtime.canvasNodeCap} value={config.canvas.maxNodes} />
          </TBody>
        </Table>
      </Section>
    </>
  );
}

const CONTENT: Record<SettingsSection, () => ReactNode> = {
  general: General,
  keys: ApiKeysPanel,
  access: AccessPanel,
  audit: AuditPanel,
};

/** Settings: appearance and configuration, and the Admin basics of the Domain. */
export function SettingsPage({ section }: { section: SettingsSection }) {
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();

  return (
    <>
      <PageHeader title={T.title} description={T.description} />
      <Tabs
        label={T.tabs}
        value={section}
        onChange={(id) => {
          void navigate({
            to: '.',
            search: { ...scopeSearch, section: isSettingsSection(id) ? id : 'general' },
          });
        }}
        tabs={SETTINGS_SECTIONS.map((id) => {
          const Content = CONTENT[id];
          return { id, label: T.sections[id], content: <Content /> };
        })}
      />
    </>
  );
}
