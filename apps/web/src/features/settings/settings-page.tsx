import { useNavigate } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { useConfig } from '@/config/context';
import { useScope, useScopeSearch } from '@/app/scope';
import { AccessPanel } from '@/features/admin/access-panel';
import { ApiKeysPanel } from '@/features/admin/api-keys-panel';
import { AuditPanel } from '@/features/admin/audit-panel';
import { ConnectionSection } from './connection-section';
import { PlannedScreens } from '@/features/planned-screens';
import { Select } from '@/ui/field';
import { PageHeader, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';
import { useTheme, type ThemeChoice } from '@/ui/theme';
import { isSettingsSection, SETTINGS_SECTIONS, type SettingsSection } from './search';

const SECTION_LABEL: Record<SettingsSection, string> = {
  general: 'General',
  keys: 'API keys',
  access: 'Principals and grants',
  audit: 'Audit log',
};

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

const searchLanguage = (settings: Record<string, unknown>): string => {
  const language = settings['search_language'];
  return typeof language === 'string' ? language : 'default';
};

function General() {
  const scope = useScope();
  const config = useConfig();
  const { theme, setTheme } = useTheme();
  const { domainInfo } = scope;

  return (
    <>
      <Section title="Appearance">
        <div className="w-48">
          <Select
            label="Theme"
            value={theme}
            options={[
              { value: 'system', label: 'System' },
              { value: 'light', label: 'Light' },
              { value: 'dark', label: 'Dark' },
            ]}
            onChange={(event) => {
              setTheme(event.target.value as ThemeChoice);
            }}
          />
        </div>
      </Section>

      <ConnectionSection />

      <Section title="Domain">
        <Table caption="Domain settings">
          <THead>
            <TR>
              <TH>Setting</TH>
              <TH>Value</TH>
            </TR>
          </THead>
          <TBody>
            <Row name="Domain id" value={domainInfo.id} />
            <Row name="Default Branch" value={domainInfo.default_branch} />
            <Row name="Created by" value={domainInfo.created_by} />
            <Row name="Search language" value={searchLanguage(domainInfo.settings)} />
          </TBody>
        </Table>
      </Section>

      <Section title="Runtime configuration">
        <Table caption="Runtime configuration">
          <THead>
            <TR>
              <TH>Setting</TH>
              <TH>Value</TH>
            </TR>
          </THead>
          <TBody>
            <Row name="Data source" value={config.apiMode} />
            <Row
              name="Keeper base URL"
              value={
                config.keeperBaseUrl === ''
                  ? 'this origin'
                  : (config.keeperBaseUrl ?? 'not used in mock mode')
              }
            />
            <Row name="Page size" value={config.pageSize} />
            <Row name="Search limit" value={config.searchLimit} />
            <Row name="Canvas depth" value={config.canvas.depth} />
            <Row name="Canvas node cap" value={config.canvas.maxNodes} />
          </TBody>
        </Table>
      </Section>

      <PlannedScreens area="History" />
      <PlannedScreens
        area="Admin"
        built={['Members and roles', 'API keys and service principals', 'Audit log']}
      />
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
      <PageHeader
        title="Settings"
        description="Appearance, runtime configuration, and who can do what in the Domain."
      />
      <Tabs
        label="Settings sections"
        value={section}
        onChange={(id) => {
          void navigate({
            to: '.',
            search: { ...scopeSearch, section: isSettingsSection(id) ? id : 'general' },
          });
        }}
        tabs={SETTINGS_SECTIONS.map((id) => {
          const Content = CONTENT[id];
          return { id, label: SECTION_LABEL[id], content: <Content /> };
        })}
      />
    </>
  );
}
