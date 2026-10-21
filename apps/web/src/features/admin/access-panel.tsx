import { useState } from 'react';
import { useGrants, usePrincipals } from '@/api/queries';
import { useScope } from '@/app/scope';
import { Select } from '@/ui/field';
import { EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { usePrincipalNames } from './principal-names';

const TYPE_FILTERS = ['all', 'user', 'service', 'api_key'] as const;
type TypeFilter = (typeof TYPE_FILTERS)[number];

const isTypeFilter = (value: string): value is TypeFilter =>
  TYPE_FILTERS.some((filter) => filter === value);

/** Principals of the installation and the grants of the Domain, read only. */
export function AccessPanel() {
  const scope = useScope();
  const [type, setType] = useState<TypeFilter>('all');
  const principals = usePrincipals(type === 'all' ? undefined : { type });
  const grants = useGrants(scope.domain);
  const nameOf = usePrincipalNames();

  return (
    <>
      <Section title="Principals">
        <div className="mb-3 w-48">
          <Select
            label="Type"
            value={type}
            options={TYPE_FILTERS.map((value) => ({
              value,
              label: value === 'all' ? 'All types' : value,
            }))}
            onChange={(event) => {
              if (isTypeFilter(event.target.value)) setType(event.target.value);
            }}
          />
        </div>
        <QueryBoundary query={principals}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>No principals of this type.</EmptyState>
            ) : (
              <Table caption="Principals">
                <THead>
                  <TR>
                    <TH>Name</TH>
                    <TH>Type</TH>
                    <TH>Id</TH>
                    <TH>External subject</TH>
                    <TH>Created</TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((principal) => (
                    <TR key={principal.id}>
                      <TD className="font-medium">{principal.display_name}</TD>
                      <TD>{principal.type}</TD>
                      <TD className="font-mono text-xs">{principal.id}</TD>
                      <TD>{principal.external_subject ?? '-'}</TD>
                      <TD>{shortTime(principal.created_at)}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )
          }
        </QueryBoundary>
      </Section>

      <Section title={`Grants in ${scope.domainInfo.display_name}`}>
        <QueryBoundary query={grants}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>No grants in this Domain.</EmptyState>
            ) : (
              <Table caption="Grants">
                <THead>
                  <TR>
                    <TH>Subject</TH>
                    <TH>Role</TH>
                    <TH>Scope</TH>
                    <TH>Granted by</TH>
                    <TH>Granted</TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((grant) => (
                    <TR key={grant.id}>
                      <TD>{nameOf(grant.subject)}</TD>
                      <TD>
                        <code className="font-mono text-xs">{grant.role}</code>
                      </TD>
                      <TD>
                        {grant.scope_type === 'domain'
                          ? 'whole Domain'
                          : `${grant.scope_type} ${grant.scope_ref ?? ''}`}
                      </TD>
                      <TD>{nameOf(grant.created_by)}</TD>
                      <TD>{shortTime(grant.created_at)}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )
          }
        </QueryBoundary>
      </Section>
    </>
  );
}
