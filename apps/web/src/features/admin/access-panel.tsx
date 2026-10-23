import { useState } from 'react';
import { useGrants, usePrincipals } from '@/api/queries';
import { useScope } from '@/app/scope';
import { admin as adminMessages } from '@/messages/admin';
import { Select } from '@/ui/field';
import { EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { usePrincipalNames } from './principal-names';

const T = adminMessages.access;

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
      <Section title={T.principals}>
        <div className="mb-3 w-48">
          <Select
            label={T.type}
            value={type}
            options={TYPE_FILTERS.map((value) => ({
              value,
              label: value === 'all' ? T.allTypes : value,
            }))}
            onChange={(event) => {
              if (isTypeFilter(event.target.value)) setType(event.target.value);
            }}
          />
        </div>
        <QueryBoundary query={principals}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>{T.noPrincipals}</EmptyState>
            ) : (
              <Table caption={T.principalsCaption}>
                <THead>
                  <TR>
                    <TH>{T.principalColumns.name}</TH>
                    <TH>{T.principalColumns.type}</TH>
                    <TH>{T.principalColumns.id}</TH>
                    <TH>{T.principalColumns.externalSubject}</TH>
                    <TH>{T.principalColumns.created}</TH>
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

      <Section title={T.grants}>
        <QueryBoundary query={grants}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState>{T.noGrants}</EmptyState>
            ) : (
              <Table caption={T.grantsCaption}>
                <THead>
                  <TR>
                    <TH>{T.grantColumns.subject}</TH>
                    <TH>{T.grantColumns.role}</TH>
                    <TH>{T.grantColumns.scope}</TH>
                    <TH>{T.grantColumns.grantedBy}</TH>
                    <TH>{T.grantColumns.granted}</TH>
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
                          ? T.wholeDomain
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
