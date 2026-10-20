import { Link } from '@tanstack/react-router';
import type { Attribute, Kind, RecordRow } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScopeSearch } from '@/app/scope';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { ValueView } from './value-view';

export interface SortState {
  attr: string;
  dir: 'asc' | 'desc';
}

function attributesOf(kind: Kind): Attribute[] {
  return Object.values(kind.attributes);
}

/** Records of one Kind with a column per Attribute. Headers sort through the URL. */
export function RecordsTable({
  domain,
  kind,
  rows,
  sort,
}: {
  domain: string;
  kind: Kind;
  rows: RecordRow[];
  sort?: SortState | undefined;
}) {
  const scopeSearch = useScopeSearch();
  const attributes = attributesOf(kind);

  return (
    <Table caption={`${kind.api_name} Records`}>
      <THead>
        <TR>
          {attributes.map((attribute) => {
            const active = sort?.attr === attribute.api_name;
            const next = active && sort.dir === 'asc' ? 'desc' : 'asc';
            return (
              <TH
                key={attribute.id}
                aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
              >
                <Link
                  to="/$domain/explorer/$kind"
                  params={{ domain, kind: kind.api_name }}
                  search={{ ...scopeSearch, sort: attribute.api_name, dir: next }}
                  className="hover:text-ink"
                >
                  {attribute.display_name}
                  {active ? (sort.dir === 'asc' ? ' (asc)' : ' (desc)') : ''}
                </Link>
              </TH>
            );
          })}
        </TR>
      </THead>
      <TBody>
        {rows.map((row) => (
          <TR key={row.ref}>
            {attributes.map((attribute) => (
              <TD key={attribute.id}>
                {attribute.id === kind.title_attribute ? (
                  <RecordLink domain={domain} recordRef={row.ref}>
                    <ValueView value={row.attributes[attribute.api_name]} type={attribute.type} />
                  </RecordLink>
                ) : (
                  <ValueView
                    value={row.attributes[attribute.api_name] ?? row.derived[attribute.api_name]}
                    type={attribute.type}
                  />
                )}
              </TD>
            ))}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
